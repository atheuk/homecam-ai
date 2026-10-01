import {afterEach,beforeEach,describe,expect,it,vi} from "vitest";
import {fireEvent,render,screen,waitFor} from "@testing-library/react";
import {MockSseStream} from "../test-setup";

const routerPush=vi.fn();
let currentSearch="";
vi.mock("next/navigation",()=>({
  useRouter:()=>({push:routerPush}),
  useSearchParams:()=>new URLSearchParams(currentSearch),
}));

import Home from "./page";

const cameras=[
  {id:"mock-front-door",name:"Front Door",type:"camera",online:true,status:"online",battery_level:null,capabilities:{}},
  {id:"mock-eufy-doorbell",name:"Front Doorbell",type:"doorbell",online:true,status:"online",battery_level:82,capabilities:{}},
];
const events=[
  {id:"evt-1",camera_id:"mock-eufy-doorbell",type:"person",priority:"high",source:"provider",
    start_time:new Date(Date.now()-60_000).toISOString(),description:"Sarah arrived",person_display_name:"Sarah"},
];

function response(body:unknown,status=200){
  return new Response(JSON.stringify(body),{status,headers:{"Content-Type":"application/json"}});
}

function mockApi(cameraData:unknown[]=cameras,eventData:unknown[]=events,liveByCamera:Record<string,unknown>={}){
  global.fetch=vi.fn(async(url:string)=>{
    const path=String(url);
    if(path.includes("/auth/login")) return response({access_token:"dashboard-token"});
    if(path.includes("/auth/me")) return response({detail:"not signed in"},401);
    if(path.includes("/auth/register")) return response({id:"new-owner"},201);
    if(path.includes("/ws")) return MockSseStream.response();
    if(path.includes("/live")){
      const id=path.match(/\/cameras\/([^/]+)\/live/)?.[1]||"";
      return liveByCamera[id]?response(liveByCamera[id]):response({message:"not found"},404);
    }
    if(path.includes("/cameras")) return response(cameraData);
    if(path.includes("/events")) return response(eventData);
    if(path.includes("/persons")) return response({persons:[]});
    return response({});
  }) as typeof fetch;
}

async function renderDashboard(){
  const rendered=render(<Home/>);
  await screen.findByLabelText("Email");
  fireEvent.change(screen.getByLabelText("Email"),{target:{value:"user@example.com"}});
  fireEvent.change(screen.getByLabelText("Password"),{target:{value:"password"}});
  fireEvent.click(screen.getByRole("button",{name:"Sign in"}));
  await screen.findByRole("tablist");
  return rendered;
}

describe("dashboard",()=>{
  beforeEach(()=>{
    currentSearch="";
    MockSseStream.instances.length=0;
    routerPush.mockReset();
    vi.spyOn(Date.prototype,"getHours").mockReturnValue(14);
    mockApi();
  });
  afterEach(()=>vi.restoreAllMocks());

  it("renders the brand and a time-aware greeting",async()=>{
    await renderDashboard();
    expect(screen.getByText("HomeCam")).toBeInTheDocument();
    expect(screen.getByRole("heading",{name:"Good afternoon."})).toBeInTheDocument();
    await screen.findByText("Front Door");
  });

  it("restores an existing browser session without persisting a bearer token",async()=>{
    mockApi();
    const api=global.fetch;
    global.fetch=vi.fn(async(url:string,init?:RequestInit)=>
      String(url).endsWith("/auth/me")?response({email:"owner@example.com"}):api(url,init)) as typeof fetch;
    render(<Home/>);
    await screen.findByText("Front Door");

    expect(global.fetch).toHaveBeenCalledWith(expect.stringContaining("/auth/me"),
      expect.objectContaining({credentials:"include",cache:"no-store"}));
    expect(global.fetch).toHaveBeenCalledWith(expect.stringContaining("/api/v1/cameras"),
      expect.objectContaining({credentials:"include",headers:{"X-HomeCam-Request":"1"}}));
    expect(screen.queryByLabelText("Password")).not.toBeInTheDocument();
    expect(window.localStorage.length).toBe(0);
    expect(window.sessionStorage.length).toBe(0);
  });

  it("shows the first-account form only after the user opts in and submits the setup secret",async()=>{
    render(<Home/>);
    await screen.findByLabelText("Email");
    expect(screen.queryByLabelText("One-time setup secret")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button",{name:"Create first account"}));
    fireEvent.change(screen.getByLabelText("Email"),{target:{value:"owner@example.com"}});
    fireEvent.change(screen.getByLabelText("Password"),{target:{value:"new-owner-password"}});
    fireEvent.change(screen.getByLabelText("One-time setup secret"),{target:{value:"owner-supplied-secret"}});
    fireEvent.click(screen.getByRole("button",{name:"Create account"}));
    await screen.findByText("Front Door");

    expect(global.fetch).toHaveBeenCalledWith(expect.stringContaining("/auth/register"),
      expect.objectContaining({
        credentials:"include",
        headers:expect.objectContaining({"X-HomeCam-Bootstrap-Secret":"owner-supplied-secret"}),
      }));
  });

  it.each([
    [8,"Good morning."],
    [14,"Good afternoon."],
    [21,"Good evening."],
  ])("uses local hour %s for %s",async(hour,greeting)=>{
    vi.mocked(Date.prototype.getHours).mockReturnValue(hour);
    await renderDashboard();
    expect(screen.getByRole("heading",{name:greeting})).toBeInTheDocument();
    await screen.findByText("Front Door");
  });

  it("renders deliberate camera and doorbell SVG icons with correct metadata",async()=>{
    const {container}=await renderDashboard();
    await screen.findByText("Front Door");
    expect(container.querySelectorAll(".camera-icon")).toHaveLength(2);
    expect(screen.getByText("camera — Connected")).toBeInTheDocument();
    expect(screen.getByText("doorbell — Connected")).toBeInTheDocument();
    expect(screen.getByText("82%")).toBeInTheDocument();
  });

  it("reports recent named activity without claiming the home is quiet",async()=>{
    await renderDashboard();
    expect(await screen.findByText("Sarah activity detected recently. All cameras are connected.")).toBeInTheDocument();
  });

  it("reports cameras that need attention while keeping dead tiles hidden",async()=>{
    mockApi([...cameras,{id:"offline",name:"Unused NVR Channel",type:"camera",online:false}],[]);
    await renderDashboard();
    expect(await screen.findByText("2 of 3 cameras online. 1 camera needs attention.")).toBeInTheDocument();
    expect(screen.queryByText("Unused NVR Channel")).not.toBeInTheDocument();
    expect(screen.getByText("Attention needed")).toBeInTheDocument();
  });

  it("shows an end-user empty state without developer instructions",async()=>{
    currentSearch="?tab=events";
    mockApi(cameras,[]);
    await renderDashboard();
    expect(await screen.findByText("No events recorded yet")).toBeInTheDocument();
    expect(screen.queryByText(/POST \/api/)).not.toBeInTheDocument();
  });

  it("loads the active tab from the URL and pushes tab changes into history",async()=>{
    currentSearch="?tab=events";
    await renderDashboard();
    const eventsTab=screen.getByRole("tab",{name:"Events"});
    expect(eventsTab).toHaveAttribute("aria-selected","true");
    await screen.findByText("Sarah arrived");

    fireEvent.click(screen.getByRole("tab",{name:"Live"}));
    expect(routerPush).toHaveBeenCalledWith("/?tab=live",{scroll:false});
    expect(screen.getByRole("tab",{name:"Live"})).toHaveAttribute("aria-selected","true");
    await screen.findAllByText(/Stream unavailable/);
  });

  it("requests only the latest 50 events and labels the metric precisely",async()=>{
    await renderDashboard();
    await screen.findByText("Front Door");
    expect(global.fetch).toHaveBeenCalledWith(expect.stringContaining("/events?limit=50"),expect.objectContaining({signal:expect.any(AbortSignal)}));
    expect(screen.getByText("LATEST EVENTS LOADED")).toBeInTheDocument();
  });

  it("shows skeletons while the initial API request is pending",async()=>{
    global.fetch=vi.fn(async(url:string)=>{
      const path=String(url);
      if(path.includes("/auth/me")) return response({},401);
      if(path.includes("/auth/login")) return response({access_token:"dashboard-token"});
      return new Promise<Response>(()=>{});
    }) as typeof fetch;
    const {container}=await renderDashboard();
    expect(screen.getByLabelText("Loading dashboard")).toHaveAttribute("aria-busy","true");
    expect(container.querySelectorAll(".skeleton-camera")).toHaveLength(2);
  });

  it("shows a retryable error when the API cannot be reached",async()=>{
    global.fetch=vi.fn(async(url:string)=>{
      const path=String(url);
      if(path.includes("/auth/me")) return response({},401);
      if(path.includes("/auth/login")) return response({access_token:"dashboard-token"});
      return Promise.reject(new Error("offline"));
    }) as typeof fetch;
    await renderDashboard();
    expect(await screen.findByRole("alert")).toHaveTextContent("HomeCam is not responding");
    expect(screen.getByRole("button",{name:"Retry"})).toBeInTheDocument();

    mockApi();
    fireEvent.click(screen.getByRole("button",{name:"Retry"}));
    expect(await screen.findByText("Front Door")).toBeInTheDocument();
  });

  it("renders events and an inline error when only camera status fails",async()=>{
    mockApi();
    const ok=global.fetch;
    global.fetch=vi.fn(async(url:string,init?:RequestInit)=>
      String(url).endsWith("/api/v1/cameras")?response({detail:"upstream"},502):ok(url,init)) as typeof fetch;
    await renderDashboard();
    expect(await screen.findByText("Sarah arrived")).toBeInTheDocument();
    expect(screen.queryByText("HomeCam is not responding")).not.toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("Camera status is unavailable.");
    expect(screen.getByRole("button",{name:"Retry"})).toBeInTheDocument();

    mockApi();
    fireEvent.click(screen.getByRole("button",{name:"Retry"}));
    expect(await screen.findByText("Front Door")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("shows the full-page error only when every source fails",async()=>{
    global.fetch=vi.fn(async(url:string)=>{
      const path=String(url);
      if(path.includes("/auth/me")) return response({},401);
      if(path.includes("/auth/login")) return response({access_token:"dashboard-token"});
      return response({detail:"down"},503);
    }) as typeof fetch;
    await renderDashboard();
    expect(await screen.findByRole("alert")).toHaveTextContent("HomeCam is not responding");
  });

  it("treats a hanging camera request as a timeout of that call only",async()=>{
    vi.useFakeTimers({shouldAdvanceTime:true});
    try{
      mockApi();
      const ok=global.fetch;
      global.fetch=vi.fn((url:string,init?:RequestInit)=>String(url).endsWith("/api/v1/cameras")
        ?new Promise<Response>((_,reject)=>init?.signal?.addEventListener("abort",()=>reject(new DOMException("Aborted","AbortError"))))
        :ok(url,init)) as typeof fetch;
      await renderDashboard();
      expect(screen.getByLabelText("Loading dashboard")).toBeInTheDocument();
      await vi.advanceTimersByTimeAsync(15_000);
      expect(await screen.findByText("Sarah arrived")).toBeInTheDocument();
      expect(screen.getByRole("alert")).toHaveTextContent("Camera status is unavailable.");
      expect(screen.queryByText("HomeCam is not responding")).not.toBeInTheDocument();
    }finally{
      vi.useRealTimers();
    }
  });

  it("keeps already-loaded events when a refresh fails",async()=>{
    currentSearch="?tab=events";
    mockApi(cameras,[{...events[0],has_photo:true,photo_url:"/api/v1/events/evt-1/photo",photo_rating:null}]);
    await renderDashboard();
    await screen.findByText("Sarah arrived");

    global.fetch=vi.fn(async(url:string)=>String(url).includes("/rating")
      ?response({})
      :Promise.reject(new Error("blip"))) as typeof fetch;
    fireEvent.click(screen.getByLabelText("Rate 4 out of 5"));

    expect(await screen.findByRole("alert")).toHaveTextContent("Events could not be refreshed. Try again.");
    expect(screen.getByText("Sarah arrived")).toBeInTheDocument();
    expect(screen.queryByText("HomeCam is not responding")).not.toBeInTheDocument();
  });

  it("renders HLS only from the API-vetted live descriptor",async()=>{
    currentSearch="?tab=live";
    mockApi(cameras,[],{
      "mock-front-door":{kind:"hls",browser_playable:true,stream_url:"https://edge.tailnet/hls/1.m3u8"},
      "mock-eufy-doorbell":{kind:"hls",browser_playable:true,stream_url:"https://edge.tailnet/hls/2.m3u8"},
    });
    const {container}=await renderDashboard();
    await waitFor(()=>expect(container.querySelector("video")).not.toBeNull());
    expect((container.querySelector("video") as HTMLVideoElement).src).toContain("hls/1.m3u8");
  });

  it("never renders a raw RTSP URL",async()=>{
    currentSearch="?tab=live";
    mockApi(cameras,[],{
      "mock-front-door":{kind:"rtsp",browser_playable:false,stream_url:"rtsp://admin:secret@192.168.1.50/cam/1"},
      "mock-eufy-doorbell":{kind:"webrtc",browser_playable:true,stream_url:"https://edge.tailnet/whep/2"},
    });
    await renderDashboard();
    expect((await screen.findAllByText(/Live preview not available in the browser/i)).length).toBeGreaterThan(0);
    expect(screen.queryByText(/rtsp:\/\//)).not.toBeInTheDocument();
    expect(screen.queryByText(/secret/)).not.toBeInTheDocument();
  });
});

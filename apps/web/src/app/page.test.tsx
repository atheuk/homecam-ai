import {afterEach,beforeEach,describe,expect,it,vi} from "vitest";
import {fireEvent,render,screen,waitFor} from "@testing-library/react";

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

describe("dashboard",()=>{
  beforeEach(()=>{
    currentSearch="";
    routerPush.mockReset();
    vi.spyOn(Date.prototype,"getHours").mockReturnValue(14);
    mockApi();
  });
  afterEach(()=>vi.restoreAllMocks());

  it("renders the brand and a time-aware greeting",async()=>{
    render(<Home/>);
    expect(screen.getByText("HomeCam")).toBeInTheDocument();
    expect(screen.getByRole("heading",{name:"Good afternoon."})).toBeInTheDocument();
    await screen.findByText("Front Door");
  });

  it.each([
    [8,"Good morning."],
    [14,"Good afternoon."],
    [21,"Good evening."],
  ])("uses local hour %s for %s",async(hour,greeting)=>{
    vi.mocked(Date.prototype.getHours).mockReturnValue(hour);
    render(<Home/>);
    expect(screen.getByRole("heading",{name:greeting})).toBeInTheDocument();
    await screen.findByText("Front Door");
  });

  it("renders deliberate camera and doorbell SVG icons with correct metadata",async()=>{
    const {container}=render(<Home/>);
    await screen.findByText("Front Door");
    expect(container.querySelectorAll(".camera-icon")).toHaveLength(2);
    expect(screen.getByText("camera — Connected")).toBeInTheDocument();
    expect(screen.getByText("doorbell — Connected")).toBeInTheDocument();
    expect(screen.getByText("82%")).toBeInTheDocument();
  });

  it("reports recent named activity without claiming the home is quiet",async()=>{
    render(<Home/>);
    expect(await screen.findByText("Sarah activity detected recently. All cameras are connected.")).toBeInTheDocument();
  });

  it("reports cameras that need attention while keeping dead tiles hidden",async()=>{
    mockApi([...cameras,{id:"offline",name:"Unused NVR Channel",type:"camera",online:false}],[]);
    render(<Home/>);
    expect(await screen.findByText("2 of 3 cameras online. 1 camera needs attention.")).toBeInTheDocument();
    expect(screen.queryByText("Unused NVR Channel")).not.toBeInTheDocument();
    expect(screen.getByText("Attention needed")).toBeInTheDocument();
  });

  it("shows an end-user empty state without developer instructions",async()=>{
    currentSearch="?tab=events";
    mockApi(cameras,[]);
    render(<Home/>);
    expect(await screen.findByText("No events recorded yet")).toBeInTheDocument();
    expect(screen.queryByText(/POST \/api/)).not.toBeInTheDocument();
  });

  it("loads the active tab from the URL and pushes tab changes into history",async()=>{
    currentSearch="?tab=events";
    render(<Home/>);
    const eventsTab=screen.getByRole("tab",{name:"Events"});
    expect(eventsTab).toHaveAttribute("aria-selected","true");
    await screen.findByText("Sarah arrived");

    fireEvent.click(screen.getByRole("tab",{name:"Live"}));
    expect(routerPush).toHaveBeenCalledWith("/?tab=live",{scroll:false});
    expect(screen.getByRole("tab",{name:"Live"})).toHaveAttribute("aria-selected","true");
    await screen.findAllByText(/Stream unavailable/);
  });

  it("requests only the latest 50 events and labels the metric precisely",async()=>{
    render(<Home/>);
    await screen.findByText("Front Door");
    expect(global.fetch).toHaveBeenCalledWith(expect.stringContaining("/events?limit=50"));
    expect(screen.getByText("LATEST EVENTS LOADED")).toBeInTheDocument();
  });

  it("shows skeletons while the initial API request is pending",()=>{
    global.fetch=vi.fn(()=>new Promise<Response>(()=>{})) as typeof fetch;
    const {container}=render(<Home/>);
    expect(screen.getByLabelText("Loading dashboard")).toHaveAttribute("aria-busy","true");
    expect(container.querySelectorAll(".skeleton-camera")).toHaveLength(2);
  });

  it("shows a retryable error when the API cannot be reached",async()=>{
    global.fetch=vi.fn(async()=>{throw new Error("offline");}) as typeof fetch;
    render(<Home/>);
    expect(await screen.findByRole("alert")).toHaveTextContent("HomeCam is not responding");
    expect(screen.getByRole("button",{name:"Retry"})).toBeInTheDocument();

    mockApi();
    fireEvent.click(screen.getByRole("button",{name:"Retry"}));
    expect(await screen.findByText("Front Door")).toBeInTheDocument();
  });

  it("renders HLS only from the API-vetted live descriptor",async()=>{
    currentSearch="?tab=live";
    mockApi(cameras,[],{
      "mock-front-door":{kind:"hls",browser_playable:true,stream_url:"https://edge.tailnet/hls/1.m3u8"},
      "mock-eufy-doorbell":{kind:"hls",browser_playable:true,stream_url:"https://edge.tailnet/hls/2.m3u8"},
    });
    const {container}=render(<Home/>);
    await waitFor(()=>expect(container.querySelector("video")).not.toBeNull());
    expect((container.querySelector("video") as HTMLVideoElement).src).toContain("hls/1.m3u8");
  });

  it("never renders a raw RTSP URL",async()=>{
    currentSearch="?tab=live";
    mockApi(cameras,[],{
      "mock-front-door":{kind:"rtsp",browser_playable:false,stream_url:"rtsp://admin:secret@192.168.1.50/cam/1"},
      "mock-eufy-doorbell":{kind:"webrtc",browser_playable:true,stream_url:"https://edge.tailnet/whep/2"},
    });
    render(<Home/>);
    expect((await screen.findAllByText(/Live preview not available in the browser/i)).length).toBeGreaterThan(0);
    expect(screen.queryByText(/rtsp:\/\//)).not.toBeInTheDocument();
    expect(screen.queryByText(/secret/)).not.toBeInTheDocument();
  });
});

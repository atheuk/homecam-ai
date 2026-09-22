import {describe,it,expect,vi,beforeEach} from "vitest";import {render,screen,fireEvent,waitFor} from "@testing-library/react";import Home from "./page";

const cameraFixture=[
  {id:"mock-front-door",name:"Front Door",type:"camera",online:true,status:"online",battery_level:null,capabilities:{}},
  {id:"mock-eufy-doorbell",name:"Front Doorbell",type:"doorbell",online:true,status:"online",battery_level:82,capabilities:{}},
];
const eventFixture=[
  {id:"evt-1",camera_id:"mock-eufy-doorbell",type:"doorbell",priority:"high",source:"provider",start_time:new Date().toISOString(),description:"Someone rang the doorbell"},
];

function mockFetchSequence(cameras:unknown[],events:unknown[],liveByCamera:Record<string,unknown>={}){
  global.fetch=vi.fn(async(url:string)=>{
    const path=String(url);
    if(path.includes("/live")){
      const match=path.match(/\/cameras\/([^/]+)\/live/);
      const id=match?match[1]:"";
      const live=liveByCamera[id];
      if(!live) return new Response("not found",{status:404});
      return new Response(JSON.stringify(live),{status:200});
    }
    if(path.includes("/cameras")) return new Response(JSON.stringify(cameras),{status:200});
    if(path.includes("/events")) return new Response(JSON.stringify(events),{status:200});
    return new Response("[]",{status:200});
  }) as typeof fetch;
}

describe("dashboard",()=>{
  beforeEach(()=>{mockFetchSequence([],[])});

  it("renders brand",()=>{
    render(<Home/>);
    expect(screen.getByText("HomeCam")).toBeInTheDocument();
  });

  it("renders discovered cameras with their online status",async()=>{
    mockFetchSequence(cameraFixture,[]);
    render(<Home/>);
    expect(await screen.findByText("Front Door")).toBeInTheDocument();
    expect(await screen.findByText("Front Doorbell")).toBeInTheDocument();
    expect(await screen.findByText("82%")).toBeInTheDocument();
  });

  it("shows an empty state when there are no events yet",async()=>{
    mockFetchSequence(cameraFixture,[]);
    render(<Home/>);
    fireEvent.click(screen.getByText("Events"));
    expect(await screen.findByText(/No events yet/i)).toBeInTheDocument();
  });

  it("lists events on the Events tab once loaded",async()=>{
    mockFetchSequence(cameraFixture,eventFixture);
    render(<Home/>);
    fireEvent.click(screen.getByText("Events"));
    expect(await screen.findByText("Someone rang the doorbell")).toBeInTheDocument();
    expect(screen.getByText("DOORBELL")).toBeInTheDocument();
  });

  it("switches between tabs without losing camera data",async()=>{
    mockFetchSequence(cameraFixture,[]);
    render(<Home/>);
    await waitFor(()=>expect(screen.getByText("Front Door")).toBeInTheDocument());
    fireEvent.click(screen.getByText("Live"));
    fireEvent.click(screen.getByText("Overview"));
    expect(screen.getByText("Front Door")).toBeInTheDocument();
  });

  it("renders an HLS video element for a browser-playable stream on the Live tab",async()=>{
    mockFetchSequence(cameraFixture,[],{
      "mock-front-door":{camera_id:"mock-front-door",kind:"hls",browser_playable:true,stream_url:"https://edge.tailnet/hls/1.m3u8"},
      "mock-eufy-doorbell":{camera_id:"mock-eufy-doorbell",kind:"hls",browser_playable:true,stream_url:"https://edge.tailnet/hls/2.m3u8"},
    });
    const {container}=render(<Home/>);
    await waitFor(()=>expect(screen.getByText("Front Door")).toBeInTheDocument());
    fireEvent.click(screen.getByText("Live"));
    await waitFor(()=>expect(container.querySelector("video")).not.toBeNull());
    const video=container.querySelector("video") as HTMLVideoElement;
    expect(video.src).toContain("hls/1.m3u8");
  });

  it("never renders a raw RTSP URL and instead shows a not-browser-playable message",async()=>{
    mockFetchSequence(cameraFixture,[],{
      "mock-front-door":{camera_id:"mock-front-door",kind:"rtsp",browser_playable:false,stream_url:"rtsp://admin:secret@192.168.1.50:554/cam/1"},
      "mock-eufy-doorbell":{camera_id:"mock-eufy-doorbell",kind:"hls",browser_playable:true,stream_url:"https://edge.tailnet/hls/2.m3u8"},
    });
    render(<Home/>);
    await waitFor(()=>expect(screen.getByText("Front Door")).toBeInTheDocument());
    fireEvent.click(screen.getByText("Live"));
    expect(await screen.findByText(/Live preview not available in the browser/i)).toBeInTheDocument();
    expect(screen.queryByText(/rtsp:\/\//)).not.toBeInTheDocument();
    expect(screen.queryByText(/secret/)).not.toBeInTheDocument();
  });

  it("shows a fallback link for a WebRTC-kind stream instead of embedding it directly",async()=>{
    mockFetchSequence(cameraFixture,[],{
      "mock-front-door":{camera_id:"mock-front-door",kind:"webrtc",browser_playable:true,stream_url:"https://edge.tailnet/whep/1"},
      "mock-eufy-doorbell":{camera_id:"mock-eufy-doorbell",kind:"hls",browser_playable:true,stream_url:"https://edge.tailnet/hls/2.m3u8"},
    });
    render(<Home/>);
    await waitFor(()=>expect(screen.getByText("Front Door")).toBeInTheDocument());
    fireEvent.click(screen.getByText("Live"));
    expect(await screen.findByText(/WebRTC client required/i)).toBeInTheDocument();
  });
});

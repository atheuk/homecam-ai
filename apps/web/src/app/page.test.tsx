import {describe,it,expect,vi,beforeEach} from "vitest";import {render,screen,fireEvent,waitFor} from "@testing-library/react";import Home from "./page";

const cameraFixture=[
  {id:"mock-front-door",name:"Front Door",type:"camera",online:true,status:"online",battery_level:null,capabilities:{}},
  {id:"mock-eufy-doorbell",name:"Front Doorbell",type:"doorbell",online:true,status:"online",battery_level:82,capabilities:{}},
];
const eventFixture=[
  {id:"evt-1",camera_id:"mock-eufy-doorbell",type:"doorbell",priority:"high",source:"provider",start_time:new Date().toISOString(),description:"Someone rang the doorbell"},
];

function mockFetchSequence(cameras:unknown[],events:unknown[]){
  global.fetch=vi.fn(async(url:string)=>{
    if(String(url).includes("/cameras")) return new Response(JSON.stringify(cameras),{status:200});
    if(String(url).includes("/events")) return new Response(JSON.stringify(events),{status:200});
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
});

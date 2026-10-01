import {describe,it,expect,vi,beforeEach,afterEach} from "vitest";import {render,screen,fireEvent,waitFor,cleanup} from "@testing-library/react";import SecurityPanel from "./SecurityPanel";
import {MockSseStream} from "../test-setup";

function jsonResponse(body:unknown,status=200){return new Response(JSON.stringify(body),{status});}

function mockFetch(handlers:Record<string,(init?:RequestInit,url?:string)=>Response>){
  global.fetch=vi.fn(async(url:string,init?:RequestInit)=>{
    const path=String(url);
    if(path.includes("/ws")) return MockSseStream.response();
    for(const [key,handler] of Object.entries(handlers)){
      if(path.includes(key)) return handler(init,path);
    }
    return jsonResponse({detail:"not found"},404);
  }) as typeof fetch;
}

const cameras=[{id:"cam-1",name:"Front Door"}];

const baseIncident={
  id:"inc-1",kind:"intrusion",status:"open",severity:"high",camera_id:"cam-1",zone:"Driveway",
  mode_at_creation:"away",event_count:2,first_seen_at:new Date().toISOString(),last_seen_at:new Date().toISOString(),
  acknowledged_by:null,acknowledged_at:null,resolved_by:null,resolved_at:null,escalation_level:0,
  summary:"Motion detected at Driveway",ai_summary:null,
};

async function signIn(){
  const result=render(<SecurityPanel cameras={cameras}/>);
  fireEvent.change(screen.getByLabelText("Email"),{target:{value:"user@example.com"}});
  fireEvent.change(screen.getByLabelText("Password"),{target:{value:"secret123"}});
  fireEvent.click(screen.getByText("Sign in"));
  await screen.findByText("Arming mode");
  return result;
}

describe("SecurityPanel",()=>{
  const originalCreateObjectURL=URL.createObjectURL;
  const originalRevokeObjectURL=URL.revokeObjectURL;

  beforeEach(()=>{
    MockSseStream.instances.length=0;
    mockFetch({
      "/auth/login":()=>jsonResponse({access_token:"tok-123",expires_at:new Date().toISOString(),user:{id:"u1",email:"user@example.com",created_at:new Date().toISOString()}}),
      "/security/mode":()=>jsonResponse({mode:"home",changed_by:"u1",changed_at:new Date().toISOString()}),
      "/security/incidents":()=>jsonResponse([baseIncident]),
      "/security/audit-log":()=>jsonResponse([]),
      "/security/schedules":()=>jsonResponse([]),
    });
  });

  afterEach(()=>{
    cleanup();
    URL.createObjectURL=originalCreateObjectURL;
    URL.revokeObjectURL=originalRevokeObjectURL;
  });

  it("shows a sign-in form before any security data is loaded", ()=>{
    render(<SecurityPanel cameras={cameras}/>);
    expect(screen.getByText("Security sign-in")).toBeInTheDocument();
    expect(screen.queryByText("Arming mode")).not.toBeInTheDocument();
  });

  it("rejects invalid credentials with an inline error", async()=>{
    mockFetch({"/auth/login":()=>jsonResponse({detail:"Invalid email or password"},401)});
    render(<SecurityPanel cameras={cameras}/>);
    fireEvent.change(screen.getByLabelText("Email"),{target:{value:"user@example.com"}});
    fireEvent.change(screen.getByLabelText("Password"),{target:{value:"wrong"}});
    fireEvent.click(screen.getByText("Sign in"));
    expect(await screen.findByText(/Invalid email or password/i)).toBeInTheDocument();
  });

  it("shows the current mode highlighted and lists open incidents after sign-in", async()=>{
    await signIn();
    await waitFor(()=>expect(screen.getByRole("button",{name:"Home"})).toHaveAttribute("aria-pressed","true"));
    expect(screen.getByText("Motion detected at Driveway")).toBeInTheDocument();
    expect(screen.getByText("1 open")).toBeInTheDocument();
  });

  it("shows clip preparation states and remains compatible with incidents without clip metadata",async()=>{
    mockFetch({
      "/auth/login":()=>jsonResponse({access_token:"tok-123",expires_at:new Date().toISOString(),user:{id:"u1",email:"e",created_at:new Date().toISOString()}}),
      "/security/mode":()=>jsonResponse({mode:"home",changed_by:"u1",changed_at:new Date().toISOString()}),
      "/security/incidents":()=>jsonResponse([
        {...baseIncident,id:"inc-pending",clip:{status:"pending",url:null}},
        {...baseIncident,id:"inc-unavailable",clip:{status:"unavailable",url:null}},
        baseIncident,
      ]),
      "/security/audit-log":()=>jsonResponse([]),
    });
    await signIn();
    expect(await screen.findByText("Incident clip is being prepared.")).toBeInTheDocument();
    expect(screen.getByText("No clip is available for this incident.")).toBeInTheDocument();
    expect(screen.queryByRole("button",{name:"Play incident clip"})).not.toBeInTheDocument();
    expect(document.querySelectorAll(".incident-clip")).toHaveLength(2);
  });

  it("fetches a ready clip only after play is requested and revokes its blob URL on unmount",async()=>{
    const clipRequests: {url:string;init?:RequestInit}[]=[];
    const createObjectURL=vi.fn().mockReturnValue("blob:incident-clip");
    const revokeObjectURL=vi.fn();
    const clipUrl="/api/v1/security/incidents/inc-1/clip?source=incident";
    URL.createObjectURL=createObjectURL as unknown as typeof URL.createObjectURL;
    URL.revokeObjectURL=revokeObjectURL as unknown as typeof URL.revokeObjectURL;
    mockFetch({
      [clipUrl]:(init)=>{
        clipRequests.push({url:`http://localhost:8000${clipUrl}`,init});
        return new Response(new Blob(["video"]),{status:200,headers:{"Content-Type":"video/mp4"}});
      },
      "/auth/login":()=>jsonResponse({access_token:"tok-123",expires_at:new Date().toISOString(),user:{id:"u1",email:"e",created_at:new Date().toISOString()}}),
      "/security/mode":()=>jsonResponse({mode:"home",changed_by:"u1",changed_at:new Date().toISOString()}),
      "/security/incidents":()=>jsonResponse([{...baseIncident,clip:{status:"ready",url:clipUrl}}]),
      "/security/audit-log":()=>jsonResponse([]),
    });
    const {unmount}=await signIn();
    expect(clipRequests).toHaveLength(0);

    fireEvent.click(await screen.findByRole("button",{name:"Play incident clip"}));
    const video=await screen.findByLabelText("Incident clip");
    expect(video).toHaveAttribute("src","blob:incident-clip");
    expect(clipRequests[0].url).toBe(`http://localhost:8000${clipUrl}`);
    expect(clipRequests[0].init?.headers).toMatchObject({Authorization:"Bearer tok-123"});
    expect(clipRequests[0].init?.credentials).toBe("include");
    expect(createObjectURL).toHaveBeenCalledOnce();

    unmount();
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:incident-clip");
  });

  it("shows a retryable error when loading a ready clip fails",async()=>{
    const clipUrl="/api/v1/security/incidents/inc-1/clip";
    mockFetch({
      [clipUrl]:()=>jsonResponse({detail:"unavailable"},503),
      "/auth/login":()=>jsonResponse({access_token:"tok-123",expires_at:new Date().toISOString(),user:{id:"u1",email:"e",created_at:new Date().toISOString()}}),
      "/security/mode":()=>jsonResponse({mode:"home",changed_by:"u1",changed_at:new Date().toISOString()}),
      "/security/incidents":()=>jsonResponse([{...baseIncident,clip:{status:"ready",url:clipUrl}}]),
      "/security/audit-log":()=>jsonResponse([]),
    });
    await signIn();
    fireEvent.click(await screen.findByRole("button",{name:"Play incident clip"}));
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not load this incident clip.");
    expect(screen.getByRole("button",{name:"Play incident clip"})).toBeEnabled();
  });

  it("updates clip retention with the schema's hold field and displays the returned hold state",async()=>{
    let held=false;
    let requestInit:RequestInit|undefined;
    const clipUrl="/api/v1/security/incidents/inc-1/clip";
    mockFetch({
      "/security/incidents/inc-1/clip/hold":(init)=>{
        requestInit=init;
        held=JSON.parse(String(init?.body)).hold;
        return jsonResponse({...baseIncident,clip:{status:"ready",url:clipUrl},clip_hold:held});
      },
      "/auth/login":()=>jsonResponse({access_token:"tok-123",expires_at:new Date().toISOString(),user:{id:"u1",email:"e",created_at:new Date().toISOString()}}),
      "/security/mode":()=>jsonResponse({mode:"home",changed_by:"u1",changed_at:new Date().toISOString()}),
      "/security/incidents":()=>jsonResponse([{...baseIncident,clip:{status:"ready",url:clipUrl},clip_hold:held}]),
      "/security/audit-log":()=>jsonResponse([]),
    });
    await signIn();
    fireEvent.click(await screen.findByRole("button",{name:"Keep clip"}));
    const kept=await screen.findByRole("button",{name:"Clip kept"});
    expect(kept).toHaveAttribute("aria-pressed","true");
    expect(requestInit?.method).toBe("PUT");
    expect(requestInit?.headers).toMatchObject({Authorization:"Bearer tok-123"});
    expect(requestInit?.credentials).toBe("include");
    expect(JSON.parse(String(requestInit?.body))).toEqual({hold:true});
  });

  it("downloads the clip through authenticated fetch with download=true in the provided clip URL",async()=>{
    const requests: {url:string;init?:RequestInit}[]=[];
    const createObjectURL=vi.fn().mockReturnValue("blob:download-clip");
    const revokeObjectURL=vi.fn();
    const click=vi.spyOn(HTMLAnchorElement.prototype,"click").mockImplementation(()=>undefined);
    const clipUrl="/api/v1/security/incidents/inc-1/clip?source=incident";
    URL.createObjectURL=createObjectURL as unknown as typeof URL.createObjectURL;
    URL.revokeObjectURL=revokeObjectURL as unknown as typeof URL.revokeObjectURL;
    mockFetch({
      "/api/v1/security/incidents/inc-1/clip":(init,url)=>{
        requests.push({url:url!,init});
        return new Response(new Blob(["video"]),{status:200,headers:{"Content-Type":"video/mp4"}});
      },
      "/auth/login":()=>jsonResponse({access_token:"tok-123",expires_at:new Date().toISOString(),user:{id:"u1",email:"e",created_at:new Date().toISOString()}}),
      "/security/mode":()=>jsonResponse({mode:"home",changed_by:"u1",changed_at:new Date().toISOString()}),
      "/security/incidents":()=>jsonResponse([{...baseIncident,clip:{status:"ready",url:clipUrl}}]),
      "/security/audit-log":()=>jsonResponse([]),
    });
    await signIn();
    expect(requests).toHaveLength(0);
    fireEvent.click(await screen.findByRole("button",{name:"Download clip"}));

    await waitFor(()=>expect(requests).toHaveLength(1));
    expect(requests[0].url).toBe("http://localhost:8000/api/v1/security/incidents/inc-1/clip?source=incident&download=true");
    expect(requests[0].url).toContain("source=incident");
    expect(requests[0].url).not.toContain("tok-123");
    expect(requests[0].init?.headers).toMatchObject({Authorization:"Bearer tok-123"});
    expect(requests[0].init?.credentials).toBe("include");
    expect(createObjectURL).toHaveBeenCalledOnce();
    expect(click).toHaveBeenCalledOnce();
    await waitFor(()=>expect(revokeObjectURL).toHaveBeenCalledWith("blob:download-clip"));
    click.mockRestore();
  });

  it("switches arming mode with a PUT request", async()=>{
    let capturedBody:Record<string,unknown>|null=null;
    mockFetch({
      "/auth/login":()=>jsonResponse({access_token:"tok-123",expires_at:new Date().toISOString(),user:{id:"u1",email:"e",created_at:new Date().toISOString()}}),
      "/security/mode":(init)=>{
        if(init?.method==="PUT"){capturedBody=JSON.parse(String(init.body));return jsonResponse({mode:capturedBody!.mode,changed_by:"u1",changed_at:new Date().toISOString()});}
        return jsonResponse({mode:"home",changed_by:"u1",changed_at:new Date().toISOString()});
      },
      "/security/incidents":()=>jsonResponse([]),
      "/security/audit-log":()=>jsonResponse([]),
    });
    await signIn();
    fireEvent.click(screen.getByRole("button",{name:"Away"}));
    await waitFor(()=>expect(capturedBody).toEqual({mode:"away"}));
    await waitFor(()=>expect(screen.getByRole("button",{name:"Away"})).toHaveAttribute("aria-pressed","true"));
  });

  it("acknowledges an open incident", async()=>{
    let acknowledged=false;
    mockFetch({
      "/auth/login":()=>jsonResponse({access_token:"tok-123",expires_at:new Date().toISOString(),user:{id:"u1",email:"e",created_at:new Date().toISOString()}}),
      "/security/mode":()=>jsonResponse({mode:"home",changed_by:"u1",changed_at:new Date().toISOString()}),
      "/security/incidents/inc-1/acknowledge":()=>{acknowledged=true;return jsonResponse({...baseIncident,status:"acknowledged"});},
      "/security/incidents":()=>jsonResponse([acknowledged?{...baseIncident,status:"acknowledged"}:baseIncident]),
      "/security/audit-log":()=>jsonResponse([]),
    });
    await signIn();
    await screen.findByText("Acknowledge");
    fireEvent.click(screen.getByText("Acknowledge"));
    await waitFor(()=>expect(acknowledged).toBe(true));
  });

  it("never renders secrets or credentials from the audit log", async()=>{
    mockFetch({
      "/auth/login":()=>jsonResponse({access_token:"tok-123",expires_at:new Date().toISOString(),user:{id:"u1",email:"e",created_at:new Date().toISOString()}}),
      "/security/mode":()=>jsonResponse({mode:"home",changed_by:"u1",changed_at:new Date().toISOString()}),
      "/security/incidents":()=>jsonResponse([]),
      "/security/audit-log":()=>jsonResponse([
        {id:"a1",actor_user_id:"u1",actor_label:"user@example.com",action:"security.mode_changed",target_type:"security_mode",target_id:"mode",details:{to:"away"},created_at:new Date().toISOString()},
      ]),
    });
    await signIn();
    const auditAction=await screen.findByText("security.mode_changed");
    // Scoped to the audit trail: other panels legitimately use words like
    // "token" in their own labels.
    const auditPanel=auditAction.closest("section");
    expect(auditPanel?.textContent).not.toMatch(/password|secret|rtsp|token/i);
  });

  it("shows a camera health banner for open camera incidents", async()=>{
    mockFetch({
      "/auth/login":()=>jsonResponse({access_token:"tok-123",expires_at:new Date().toISOString(),user:{id:"u1",email:"e",created_at:new Date().toISOString()}}),
      "/security/mode":()=>jsonResponse({mode:"home",changed_by:"u1",changed_at:new Date().toISOString()}),
      "/security/incidents":()=>jsonResponse([{...baseIncident,id:"inc-2",kind:"camera_offline",zone:null,severity:"high"}]),
      "/security/audit-log":()=>jsonResponse([]),
    });
    await signIn();
    expect(await screen.findByText(/need attention/i)).toBeInTheDocument();
    expect(screen.getByText(/Front Door \(Camera offline\)/)).toBeInTheDocument();
  });

  it("refreshes the incident list live when an SSE incident.created event arrives, with no reload or manual action", async()=>{
    let incidentsCallCount=0;
    mockFetch({
      "/auth/login":()=>jsonResponse({access_token:"tok-123",expires_at:new Date().toISOString(),user:{id:"u1",email:"e",created_at:new Date().toISOString()}}),
      "/security/mode":()=>jsonResponse({mode:"home",changed_by:"u1",changed_at:new Date().toISOString()}),
      "/security/incidents":()=>{
        incidentsCallCount+=1;
        // The second (and later) fetch, triggered by the SSE push, sees a
        // brand-new incident the first load never returned.
        return jsonResponse(incidentsCallCount===1?[]:[baseIncident]);
      },
      "/security/audit-log":()=>jsonResponse([]),
    });
    await signIn();
    await screen.findByText("Nothing needs attention right now.");
    expect(screen.queryByText("Motion detected at Driveway")).not.toBeInTheDocument();
    expect(screen.getByText("0 open")).toBeInTheDocument();

    const source=MockSseStream.instances.at(-1);
    expect(source).toBeDefined();
    source!.dispatch("incident.created",{...baseIncident});

    expect(await screen.findByText("Motion detected at Driveway")).toBeInTheDocument();
    await waitFor(()=>expect(screen.getByText("1 open")).toBeInTheDocument());
  });

  it("refreshes on incident.updated and incident.escalated SSE events too", async()=>{
    let incidentsCallCount=0;
    mockFetch({
      "/auth/login":()=>jsonResponse({access_token:"tok-123",expires_at:new Date().toISOString(),user:{id:"u1",email:"e",created_at:new Date().toISOString()}}),
      "/security/mode":()=>jsonResponse({mode:"home",changed_by:"u1",changed_at:new Date().toISOString()}),
      "/security/incidents":()=>{
        incidentsCallCount+=1;
        return jsonResponse(incidentsCallCount<3?[baseIncident]:[{...baseIncident,escalation_level:1}]);
      },
      "/security/audit-log":()=>jsonResponse([]),
    });
    await signIn();
    const source=MockSseStream.instances.at(-1);
    expect(source).toBeDefined();

    source!.dispatch("incident.updated",{...baseIncident});
    await waitFor(()=>expect(incidentsCallCount).toBeGreaterThanOrEqual(2));

    source!.dispatch("incident.escalated",{...baseIncident,escalation_level:1});
    await screen.findByText("escalated ×1");
  });

  const scheduleStatus={
    enabled:true,timezone:"Europe/Amsterdam",scheduled_mode:"night",
    active_schedule_id:"s1",active_schedule_name:"Nights",
    next_transition_at:new Date("2025-01-02T07:00:00Z").toISOString(),next_transition_mode:"disarmed",
    override_active:true,
  };
  const nightSchedule={
    id:"s1",name:"Nights",mode:"night",days_of_week:[0,1,2,3,4,5,6],
    start_time:"23:00",end_time:"07:00",enabled:true,priority:0,
  };

  function mockWithSchedules(scheduleHandler:(init?:RequestInit)=>Response){
    mockFetch({
      "/auth/login":()=>jsonResponse({access_token:"tok-123",expires_at:new Date().toISOString(),user:{id:"u1",email:"e",created_at:new Date().toISOString()}}),
      "/security/mode":()=>jsonResponse({mode:"home",changed_by:"u1",changed_at:new Date().toISOString(),changed_source:"manual",schedule:scheduleStatus}),
      "/security/incidents":()=>jsonResponse([]),
      "/security/audit-log":()=>jsonResponse([]),
      "/security/schedules":scheduleHandler,
    });
  }

  it("lists arming schedules with the next transition and override state", async()=>{
    mockWithSchedules(()=>jsonResponse([nightSchedule]));
    await signIn();
    await screen.findByText("Nights");
    expect(screen.getByText(/night 23:00-07:00/)).toBeInTheDocument();
    expect(screen.getByText("(Every day)")).toBeInTheDocument();
    const status=await screen.findByTestId("schedule-status");
    expect(status).toHaveTextContent("Manual override is active until then.");
  });

  it("creates a schedule from the Security tab", async()=>{
    let created:Record<string,unknown>|null=null;
    mockWithSchedules((init)=>{
      if(init?.method==="POST"){created=JSON.parse(String(init.body));return jsonResponse({...nightSchedule,...created});}
      return jsonResponse(created?[{...nightSchedule,...created}]:[]);
    });
    await signIn();
    fireEvent.click(await screen.findByRole("button",{name:"Add schedule"}));
    fireEvent.change(screen.getByLabelText("Name"),{target:{value:"Workdays"}});
    fireEvent.change(screen.getByLabelText("Mode"),{target:{value:"away"}});
    fireEvent.change(screen.getByLabelText("Start"),{target:{value:"09:00"}});
    fireEvent.change(screen.getByLabelText("End"),{target:{value:"17:00"}});
    for(const day of ["Sat","Sun"]) fireEvent.click(screen.getByRole("button",{name:day}));
    fireEvent.click(screen.getByText("Save schedule"));
    await waitFor(()=>expect(created).toEqual({
      name:"Workdays",mode:"away",days_of_week:[0,1,2,3,4],start_time:"09:00",end_time:"17:00",
    }));
  });

  it("deletes a schedule", async()=>{
    let deleted=false;
    mockWithSchedules((init)=>{
      if(init?.method==="DELETE"){deleted=true;return jsonResponse({});}
      return jsonResponse(deleted?[]:[nightSchedule]);
    });
    await signIn();
    fireEvent.click(await screen.findByRole("button",{name:"Delete"}));
    await waitFor(()=>expect(deleted).toBe(true));
    await screen.findByText(/No schedules yet/);
  });
});

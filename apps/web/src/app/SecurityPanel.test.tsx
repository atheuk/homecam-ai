import {describe,it,expect,vi,beforeEach} from "vitest";import {render,screen,fireEvent,waitFor} from "@testing-library/react";import SecurityPanel from "./SecurityPanel";
import {MockSseStream} from "../test-setup";

function jsonResponse(body:unknown,status=200){return new Response(JSON.stringify(body),{status});}

function mockFetch(handlers:Record<string,(init?:RequestInit)=>Response>){
  global.fetch=vi.fn(async(url:string,init?:RequestInit)=>{
    const path=String(url);
    if(path.includes("/ws")) return MockSseStream.response();
    for(const [key,handler] of Object.entries(handlers)){
      if(path.includes(key)) return handler(init);
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
  render(<SecurityPanel cameras={cameras}/>);
  fireEvent.change(screen.getByLabelText("Email"),{target:{value:"user@example.com"}});
  fireEvent.change(screen.getByLabelText("Password"),{target:{value:"secret123"}});
  fireEvent.click(screen.getByText("Sign in"));
  await screen.findByText("Arming mode");
}

describe("SecurityPanel",()=>{
  beforeEach(()=>{
    MockSseStream.instances.length=0;
    mockFetch({
      "/auth/login":()=>jsonResponse({access_token:"tok-123",expires_at:new Date().toISOString(),user:{id:"u1",email:"user@example.com",created_at:new Date().toISOString()}}),
      "/security/mode":()=>jsonResponse({mode:"home",changed_by:"u1",changed_at:new Date().toISOString()}),
      "/security/incidents":()=>jsonResponse([baseIncident]),
      "/security/audit-log":()=>jsonResponse([]),
    });
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
    expect(await screen.findByText("security.mode_changed")).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/password|secret|rtsp|token/i);
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
});

import {describe,it,expect,vi,beforeEach} from "vitest";import {render,screen,fireEvent,waitFor} from "@testing-library/react";import AdminPanel from "./AdminPanel";

function jsonResponse(body:unknown,status=200){return new Response(JSON.stringify(body),{status});}

function mockFetch(handlers:Record<string,(init?:RequestInit)=>Response>){
  global.fetch=vi.fn(async(url:string,init?:RequestInit)=>{
    const path=String(url);
    for(const [key,handler] of Object.entries(handlers)){
      if(path.includes(key)) return handler(init);
    }
    return jsonResponse({detail:"not found"},404);
  }) as typeof fetch;
}

async function signIn(){
  render(<AdminPanel/>);
  fireEvent.change(screen.getByLabelText("Email"),{target:{value:"user@example.com"}});
  fireEvent.change(screen.getByLabelText("Password"),{target:{value:"secret123"}});
  fireEvent.click(screen.getByText("Sign in"));
  await screen.findByText("Dahua NVR connection");
}

describe("AdminPanel",()=>{
  beforeEach(()=>{
    mockFetch({
      "/auth/login":()=>jsonResponse({access_token:"tok-123",expires_at:new Date().toISOString(),user:{id:"u1",email:"user@example.com",created_at:new Date().toISOString()}}),
      "/admin/providers":()=>jsonResponse([]),
    });
  });

  it("shows a sign-in form before any admin data is loaded", ()=>{
    render(<AdminPanel/>);
    expect(screen.getByText("Admin sign-in")).toBeInTheDocument();
    expect(screen.queryByText("Dahua NVR connection")).not.toBeInTheDocument();
  });

  it("rejects invalid credentials with an inline error", async()=>{
    mockFetch({"/auth/login":()=>jsonResponse({detail:"Invalid email or password"},401)});
    render(<AdminPanel/>);
    fireEvent.change(screen.getByLabelText("Email"),{target:{value:"user@example.com"}});
    fireEvent.change(screen.getByLabelText("Password"),{target:{value:"wrong"}});
    fireEvent.click(screen.getByText("Sign in"));
    expect(await screen.findByText(/Invalid email or password/i)).toBeInTheDocument();
  });

  it("renders the Dahua and Eufy forms plus provider list after sign-in", async()=>{
    await signIn();
    expect(screen.getByText("Eufy adapter connection")).toBeInTheDocument();
    expect(screen.getByText("Configured providers")).toBeInTheDocument();
    expect(screen.getByText("No providers configured yet.")).toBeInTheDocument();
  });

  it("masks the Dahua password and Eufy token as password inputs", async()=>{
    await signIn();
    const dahuaPassword=screen.getByLabelText("Password") as HTMLInputElement;
    expect(dahuaPassword.type).toBe("password");
    const eufyToken=screen.getByLabelText("Adapter token") as HTMLInputElement;
    expect(eufyToken.type).toBe("password");
  });

  it("supports adding and removing channel rows", async()=>{
    await signIn();
    expect(screen.getAllByLabelText(/Channel \d+ number/)).toHaveLength(1);
    fireEvent.click(screen.getByText("Add channel"));
    expect(screen.getAllByLabelText(/Channel \d+ number/)).toHaveLength(2);
    fireEvent.click(screen.getAllByText("Remove")[0]);
    expect(screen.getAllByLabelText(/Channel \d+ number/)).toHaveLength(1);
  });

  it("submits the Dahua form without leaking the password field name as its value", async()=>{
    let capturedBody:Record<string,unknown>|null=null;
    mockFetch({
      "/auth/login":()=>jsonResponse({access_token:"tok-123",expires_at:new Date().toISOString(),user:{id:"u1",email:"e",created_at:new Date().toISOString()}}),
      "/admin/providers/dahua/test":()=>jsonResponse({success:false,status:"UNREACHABLE",message:"Could not reach host"}),
      "/admin/providers/dahua":(init)=>{capturedBody=JSON.parse(String(init?.body));return jsonResponse({id:"cfg-1",provider_type:"dahua",name:"Dahua NVR",enabled:true,scheme:"http",host:"192.0.2.10",port:80,username:"admin",channels:"1:Front Door",adapter_url:null,has_secret:true,last_test_status:null,last_test_message:null,last_test_at:null,created_at:new Date().toISOString(),updated_at:new Date().toISOString()},201);},
      "/admin/providers":()=>jsonResponse([]),
    });
    await signIn();
    fireEvent.change(screen.getByLabelText("Host"),{target:{value:"192.0.2.10"}});
    fireEvent.change(screen.getByLabelText("Username"),{target:{value:"admin"}});
    fireEvent.change(screen.getByLabelText("Password"),{target:{value:"hunter2"}});
    fireEvent.click(screen.getAllByText("Save")[0]);
    await waitFor(()=>expect(capturedBody).not.toBeNull());
    expect(capturedBody!.password).toBe("hunter2");
    expect(capturedBody!.host).toBe("192.0.2.10");
    await screen.findByText("Saved.");
  });

  it("runs a Dahua test connection and surfaces failure without echoing secrets", async()=>{
    mockFetch({
      "/auth/login":()=>jsonResponse({access_token:"tok-123",expires_at:new Date().toISOString(),user:{id:"u1",email:"e",created_at:new Date().toISOString()}}),
      "/admin/providers/dahua/test":()=>jsonResponse({success:false,status:"UNREACHABLE",message:"Could not reach host"}),
      "/admin/providers":()=>jsonResponse([]),
    });
    await signIn();
    fireEvent.change(screen.getByLabelText("Host"),{target:{value:"192.0.2.10"}});
    fireEvent.change(screen.getByLabelText("Username"),{target:{value:"admin"}});
    fireEvent.change(screen.getByLabelText("Password"),{target:{value:"hunter2"}});
    fireEvent.click(screen.getAllByText("Test Connection")[0]);
    expect(await screen.findByText(/Could not reach host/i)).toBeInTheDocument();
    expect(screen.queryByText("hunter2")).not.toBeInTheDocument();
  });

  it("lists a configured provider with redacted fields and enable/disable/delete actions", async()=>{    mockFetch({
      "/auth/login":()=>jsonResponse({access_token:"tok-123",expires_at:new Date().toISOString(),user:{id:"u1",email:"e",created_at:new Date().toISOString()}}),
      "/admin/providers/cfg-1/enabled":()=>jsonResponse({id:"cfg-1",provider_type:"dahua",name:"Dahua NVR",enabled:false,scheme:"http",host:"192.0.2.10",port:80,username:"admin",channels:"1:Front Door",adapter_url:null,has_secret:true,last_test_status:"SUCCESS",last_test_message:"ok",last_test_at:new Date().toISOString(),created_at:new Date().toISOString(),updated_at:new Date().toISOString()}),
      "/admin/providers":()=>jsonResponse([{id:"cfg-1",provider_type:"dahua",name:"Dahua NVR",enabled:true,scheme:"http",host:"192.0.2.10",port:80,username:"admin",channels:"1:Front Door",adapter_url:null,has_secret:true,last_test_status:"SUCCESS",last_test_message:"ok",last_test_at:new Date().toISOString(),created_at:new Date().toISOString(),updated_at:new Date().toISOString()}]),
    });
    await signIn();
    expect(await screen.findByText("http://192.0.2.10:80")).toBeInTheDocument();
    expect(screen.getByText("Enabled")).toBeInTheDocument();
    expect(screen.getByText("Last test: SUCCESS")).toBeInTheDocument();
    expect(screen.queryByText("hunter2")).not.toBeInTheDocument();
    fireEvent.click(screen.getByText("Disable"));
    await waitFor(()=>expect(global.fetch).toHaveBeenCalledWith(expect.stringContaining("/admin/providers/cfg-1/enabled"),expect.objectContaining({method:"POST"})));
  });

  it("switches the Dahua form to edge-connector mode and hides direct-mode fields", async()=>{
    await signIn();
    expect(screen.getByLabelText("Host")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Connection mode"),{target:{value:"edge"}});
    expect(screen.queryByLabelText("Host")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Username")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Edge connector base URL")).toBeInTheDocument();
    expect(screen.getByLabelText("Edge connector token")).toBeInTheDocument();
  });

  it("submits an edge-mode Dahua config with edge_base_url/edge_token and no direct-mode fields", async()=>{
    let capturedBody:Record<string,unknown>|null=null;
    mockFetch({
      "/auth/login":()=>jsonResponse({access_token:"tok-123",expires_at:new Date().toISOString(),user:{id:"u1",email:"e",created_at:new Date().toISOString()}}),
      "/admin/providers/dahua":(init)=>{capturedBody=JSON.parse(String(init?.body));return jsonResponse({id:"cfg-2",provider_type:"dahua",name:"Pi Edge Connector",enabled:true,mode:"edge",adapter_url:"https://pi.tailnet.ts.net:8443",has_secret:true,last_test_status:null,last_test_message:null,last_test_at:null,created_at:new Date().toISOString(),updated_at:new Date().toISOString()},201);},
      "/admin/providers":()=>jsonResponse([]),
    });
    await signIn();
    fireEvent.change(screen.getByLabelText("Connection mode"),{target:{value:"edge"}});
    fireEvent.change(screen.getByLabelText("Edge connector base URL"),{target:{value:"https://pi.tailnet.ts.net:8443"}});
    fireEvent.change(screen.getByLabelText("Edge connector token"),{target:{value:"edge-secret-token"}});
    fireEvent.click(screen.getAllByText("Save")[0]);
    await waitFor(()=>expect(capturedBody).not.toBeNull());
    expect(capturedBody!.mode).toBe("edge");
    expect(capturedBody!.edge_base_url).toBe("https://pi.tailnet.ts.net:8443");
    expect(capturedBody!.edge_token).toBe("edge-secret-token");
    expect(capturedBody!.host).toBeUndefined();
    expect(capturedBody!.password).toBeUndefined();
    await screen.findByText("Saved.");
  });

  it("shows the stored mode in the configured providers list", async()=>{
    mockFetch({
      "/auth/login":()=>jsonResponse({access_token:"tok-123",expires_at:new Date().toISOString(),user:{id:"u1",email:"e",created_at:new Date().toISOString()}}),
      "/admin/providers":()=>jsonResponse([{id:"cfg-2",provider_type:"dahua",name:"Pi Edge Connector",enabled:true,mode:"edge",adapter_url:"https://pi.tailnet.ts.net:8443",has_secret:true,last_test_status:null,last_test_message:null,last_test_at:null,created_at:new Date().toISOString(),updated_at:new Date().toISOString()}]),
    });
    await signIn();
    expect(await screen.findByText("dahua (edge)")).toBeInTheDocument();
    expect(screen.getByText("https://pi.tailnet.ts.net:8443")).toBeInTheDocument();
  });

  const zoneHandlers={
    "/auth/login":()=>jsonResponse({access_token:"tok-123",expires_at:new Date().toISOString(),user:{id:"u1",email:"e",created_at:new Date().toISOString()}}),
    "/api/v1/cameras":()=>jsonResponse([{id:"mock-front-door",name:"Front Door"}]),
    "/admin/providers":()=>jsonResponse([]),
  };

  it("lists the detection zones configured for the selected camera", async()=>{
    mockFetch({
      ...zoneHandlers,
      "/admin/cameras/mock-front-door/zones":()=>jsonResponse([{id:"z1",camera_id:"mock-front-door",name:"driveway",kind:"driveway",x1:0,y1:0.5,x2:0.6,y2:1}]),
    });
    await signIn();
    expect(await screen.findByText(/driveway — \[0, 0.5\]/)).toBeInTheDocument();
  });

  it("posts a new zone with normalized coordinates", async()=>{
    let capturedBody:Record<string,unknown>|null=null;
    mockFetch({
      ...zoneHandlers,
      "/admin/cameras/mock-front-door/zones":(init)=>{
        if(init?.method==="POST"){capturedBody=JSON.parse(String(init.body));return jsonResponse({id:"z1"},201);}
        return jsonResponse([]);
      },
    });
    await signIn();
    await screen.findByText("Detection zones");
    fireEvent.change(screen.getByLabelText("Zone name"),{target:{value:"mailbox"}});
    fireEvent.change(screen.getByLabelText("Zone x1"),{target:{value:"0.7"}});
    fireEvent.click(screen.getByText("Add zone"));
    await waitFor(()=>expect(capturedBody).not.toBeNull());
    expect(capturedBody!.name).toBe("mailbox");
    expect(capturedBody!.x1).toBe(0.7);
    await screen.findByText("Zone saved.");
  });

  it("reports an inline error when a zone rectangle is rejected", async()=>{
    mockFetch({
      ...zoneHandlers,
      "/admin/cameras/mock-front-door/zones":(init)=>init?.method==="POST"?jsonResponse({detail:"invalid"},422):jsonResponse([]),
    });
    await signIn();
    await screen.findByText("Detection zones");
    fireEvent.click(screen.getByText("Add zone"));
    expect(await screen.findByText(/Coordinates must be between 0 and 1/)).toBeInTheDocument();
  });
});

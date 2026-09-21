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

  it("lists a configured provider with redacted fields and enable/disable/delete actions", async()=>{
    mockFetch({
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
});

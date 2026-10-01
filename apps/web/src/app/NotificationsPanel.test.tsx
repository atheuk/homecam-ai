import {describe,it,expect,vi,beforeEach} from "vitest";import {render,screen,fireEvent,waitFor} from "@testing-library/react";import NotificationsPanel from "./NotificationsPanel";

function jsonResponse(body:unknown,status=200){return new Response(JSON.stringify(body),{status});}

const status={
  notifications_enabled:true,web_push_available:false,web_push_detail:"VAPID keys are not configured",
  vapid_public_key:null,deep_links_configured:true,channel_count:2,enabled_channel_count:1,subscription_count:0,
};

const settings={
  enabled:true,quiet_hours_enabled:false,quiet_hours_start:"22:00",quiet_hours_end:"07:00",
  quiet_hours_override_severity:"critical",min_severity:"medium",max_per_hour:10,
};

const ntfyChannel={
  id:"ch-1",type:"ntfy",name:"Phone",enabled:true,min_severity:"medium",attach_images:false,
  config:{topic:"home-alerts",server:"https://ntfy.sh"},has_secret:true,
  last_status:"sent",last_message:null,last_sent_at:new Date().toISOString(),
};

const webpushChannel={
  id:"ch-2",type:"webpush",name:"Browsers",enabled:false,min_severity:"high",attach_images:false,
  config:{},has_secret:false,last_status:null,last_message:null,last_sent_at:null,
};

function mockFetch(overrides:Record<string,(init?:RequestInit)=>Response>={}){
  const handlers:Record<string,(init?:RequestInit)=>Response>={
    "/notifications/status":()=>jsonResponse(status),
    "/notifications/settings":()=>jsonResponse(settings),
    "/notifications/channels":()=>jsonResponse([ntfyChannel,webpushChannel]),
    ...overrides,
  };
  const spy=vi.fn(async(url:string,init?:RequestInit)=>{
    const path=String(url);
    const key=Object.keys(handlers).sort((a,b)=>b.length-a.length).find(k=>path.includes(k));
    return key?handlers[key](init):jsonResponse({detail:"not found"},404);
  });
  global.fetch=spy as unknown as typeof fetch;
  return spy;
}

describe("NotificationsPanel",()=>{
  beforeEach(()=>{mockFetch();});

  it("lists configured channels with their transport",async()=>{
    render(<NotificationsPanel token="tok"/>);
    expect(await screen.findByText("Phone")).toBeInTheDocument();
    expect(screen.getAllByText("ntfy").length).toBeGreaterThan(0);
    expect(screen.getByText("Browsers")).toBeInTheDocument();
  });

  it("offers a snapshot opt-in only for authenticated transports",async()=>{
    render(<NotificationsPanel token="tok"/>);
    await screen.findByText("Phone");
    // One checkbox for the ntfy channel; the web push channel must not get one.
    expect(screen.getAllByLabelText("Attach snapshot")).toHaveLength(1);
  });

  it("never renders a secret in a readable field",async()=>{
    render(<NotificationsPanel token="tok"/>);
    await screen.findByText("Phone");
    fireEvent.change(screen.getByLabelText("Type"),{target:{value:"telegram"}});
    const tokenField=screen.getByLabelText("Bot token") as HTMLInputElement;
    expect(tokenField.type).toBe("password");
    expect(tokenField.value).toBe("");
    expect(document.body.textContent).not.toContain("has_secret");
  });

  it("hides the subscribe button when the browser cannot do web push",async()=>{
    render(<NotificationsPanel token="tok"/>);
    await screen.findByText("Phone");
    expect(screen.queryByText("Enable alerts in this browser")).not.toBeInTheDocument();
    expect(screen.getByText(/does not support web push/)).toBeInTheDocument();
  });

  it("hides the subscribe button when web push is not configured on the server",async()=>{
    vi.stubGlobal("PushManager",class{});
    vi.stubGlobal("Notification",class{static requestPermission(){return Promise.resolve("granted");}});
    Object.defineProperty(navigator,"serviceWorker",{
      configurable:true,
      value:{getRegistration:async()=>undefined,register:async()=>({}),ready:Promise.resolve({})},
    });
    try{
      render(<NotificationsPanel token="tok"/>);
      await screen.findByText("Phone");
      expect(screen.queryByText("Enable alerts in this browser")).not.toBeInTheDocument();
      expect(await screen.findByText(/Web push is not configured on the server/)).toBeInTheDocument();
    }finally{
      vi.unstubAllGlobals();
      Reflect.deleteProperty(navigator,"serviceWorker");
    }
  });

  it("saves a settings change back to the API",async()=>{
    const spy=mockFetch({"/notifications/settings":()=>jsonResponse(settings)});
    render(<NotificationsPanel token="tok"/>);
    await screen.findByText("Phone");
    fireEvent.change(screen.getByLabelText("Minimum severity"),{target:{value:"high"}});
    await waitFor(()=>{
      const put=spy.mock.calls.find(([,init])=>(init as RequestInit|undefined)?.method==="PUT");
      expect(put).toBeTruthy();
      expect(JSON.parse(String((put?.[1] as RequestInit).body))).toMatchObject({min_severity:"high"});
    });
  });

  it("reports a failed test send without exposing channel configuration",async()=>{
    mockFetch({"/notifications/channels/ch-1/test":()=>jsonResponse({status:"failed",detail:"topic rejected"})});
    render(<NotificationsPanel token="tok"/>);
    await screen.findByText("Phone");
    fireEvent.click(screen.getAllByText("Send test")[0]);
    expect(await screen.findByText(/Test failed: topic rejected/)).toBeInTheDocument();
  });

  it("surfaces a load failure instead of rendering an empty form",async()=>{
    mockFetch({"/notifications/status":()=>jsonResponse({detail:"nope"},401)});
    render(<NotificationsPanel token={null}/>);
    expect(await screen.findByText("Could not load notification settings.")).toBeInTheDocument();
  });
});

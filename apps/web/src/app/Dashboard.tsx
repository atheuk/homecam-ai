"use client";

import {useCallback,useEffect,useMemo,useState,type FormEvent} from "react";
import {useRouter,useSearchParams} from "next/navigation";
import AdminPanel from "./AdminPanel";
import EventsPanel from "./EventsPanel";
import SecurityPanel from "./SecurityPanel";
import {HlsVideo} from "./Player";
import {EventCard,PeoplePanel,type EventItem,type Person} from "./People";
import {consumeSse,SseResponseError} from "./sse";

const API=process.env.NEXT_PUBLIC_API_URL||"http://localhost:8000";
const EVENT_LIMIT=50;
const ACTIVITY_WINDOW_MS=10*60*1000;
const TABS=["Overview","Live","Events","People","Security","System","Settings"] as const;
type Tab=typeof TABS[number];

// Fixed codes the API's Google callback redirects back with. Anything else
// is ignored, so a crafted link cannot inject text into the sign-in page.
export const GOOGLE_ERROR_MESSAGES:Record<string,string>={
  pending_approval:"Your Google account is registered but has not been approved yet. Ask the HomeCam owner for access.",
  account_disabled:"This account has been disabled.",
  account_exists:"An account with this email already exists. Sign in with your password, then link Google from System → Account.",
  account_conflict:"That Google account could not be registered. Try again or contact the owner.",
  email_not_verified:"Google has not verified this email address, so it cannot be used to sign in.",
  access_denied:"Google sign-in was cancelled.",
  invalid_state:"The Google sign-in request was invalid or already used. Start again.",
  expired_state:"The Google sign-in request expired. Start again.",
  invalid_token:"Google's response could not be verified. Start again.",
  token_exchange_failed:"HomeCam could not complete sign-in with Google. Try again.",
  link_expired:"The Google link request expired. Start again from System → Account.",
  link_requires_session:"Sign in to HomeCam in this browser before linking a Google account.",
  already_linked_other:"This HomeCam account is already linked to a different Google account.",
  google_account_in_use:"That Google account is already linked to another HomeCam account.",
};

export function googleNotice(params:{get(name:string):string|null}){
  const code=params.get("google_error");
  if(code) return {error:true,text:GOOGLE_ERROR_MESSAGES[code]||"Google sign-in failed. Try again."};
  if(params.get("google")==="linked") return {error:false,text:"Google account linked. You can now sign in with Google."};
  return null;
}

export type Camera={
  id:string;
  name:string;
  type:string;
  online:boolean;
  battery_level?:number|null;
};

type LiveStream=
  |{kind:string;browser_playable:boolean;stream_url:string}
  |{error:string};

export function greetingForHour(hour:number){
  if(hour<12) return "Good morning.";
  if(hour<18) return "Good afternoon.";
  return "Good evening.";
}

export function homeStatus(cameras:Camera[],events:EventItem[],now=Date.now()){
  if(!cameras.length) return "No cameras are reporting. Check your camera connections.";
  const online=cameras.filter(camera=>camera.online).length;
  const offline=cameras.length-online;
  if(offline>0){
    return `${online} of ${cameras.length} cameras online. ${offline} ${offline===1?"camera needs":"cameras need"} attention.`;
  }
  const latest=events[0];
  if(latest&&now-new Date(latest.start_time).getTime()<=ACTIVITY_WINDOW_MS){
    const subject=latest.person_display_name||latest.type;
    return `${subject.charAt(0).toUpperCase()+subject.slice(1)} activity detected recently. All cameras are connected.`;
  }
  return "No activity in the last 10 minutes. All cameras are connected.";
}

function CameraIcon({type}:{type:string}){
  if(type==="doorbell"){
    return <svg className="camera-icon" viewBox="0 0 48 48" aria-hidden="true">
      <rect x="13" y="4" width="22" height="40" rx="8"/>
      <circle cx="24" cy="16" r="5"/>
      <circle cx="24" cy="33" r="3"/>
    </svg>;
  }
  return <svg className="camera-icon" viewBox="0 0 48 48" aria-hidden="true">
    <path d="M7 17h26a6 6 0 0 1 6 6v10H13a6 6 0 0 1-6-6V17Z"/>
    <circle cx="30" cy="25" r="6"/>
    <path d="M17 33v7M12 40h10"/>
  </svg>;
}

function CameraGrid({cameras}:{cameras:Camera[]}){
  if(!cameras.length){
    return <div className="empty-state camera-empty"><strong>No connected cameras</strong><p>Camera tiles will appear when a channel comes online.</p></div>;
  }
  return <section className="grid" aria-label="Connected cameras">{cameras.map(camera=>
    <article className="camera" key={camera.id}>
      <div className="camera-art">
        <CameraIcon type={camera.type}/>
        <span className="live-label">LIVE</span>
      </div>
      <div className="camera-meta">
        <div><h3>{camera.name}</h3><p>{camera.type} — Connected</p></div>
        {camera.battery_level!=null&&<span className="battery">{camera.battery_level}%</span>}
      </div>
    </article>
  )}</section>;
}

/** Fetches only browser-safe stream descriptors vetted by the API. Raw RTSP
 * URLs and credentials are never rendered or reconstructed in this client. */
export function LiveView({cameras,token}:{cameras:Camera[];token:string|null}){
  const [streams,setStreams]=useState<Record<string,LiveStream>>({});
  useEffect(()=>{
    let cancelled=false;
    cameras.forEach(camera=>{
      fetch(`${API}/api/v1/cameras/${camera.id}/live`,{
        headers:{...(token?{Authorization:`Bearer ${token}`}:{ }),"X-HomeCam-Request":"1"},
        credentials:"include",
      }).then(async response=>{
        if(!response.ok){
          if(!cancelled) setStreams(current=>({...current,[camera.id]:{error:`Stream unavailable (HTTP ${response.status}).`}}));
          return;
        }
        const body=await response.json();
        if(!cancelled) setStreams(current=>({...current,[camera.id]:{
          kind:body.kind,browser_playable:body.browser_playable,stream_url:body.stream_url,
        }}));
      }).catch(()=>{
        if(!cancelled) setStreams(current=>({...current,[camera.id]:{error:"Stream unavailable."}}));
      });
    });
    return ()=>{cancelled=true;};
  },[cameras,token]);

  if(!cameras.length) return <div className="empty-state"><strong>No connected cameras</strong><p>Live streams will appear when a camera comes online.</p></div>;
  return <section className="grid" id="panel-live" role="tabpanel" aria-labelledby="tab-live">
    {cameras.map(camera=>{
      const stream=streams[camera.id];
      return <article className="camera" key={camera.id}>
        <div className="camera-art stream-art">
          {!stream?<div className="stream-loading"><span className="spinner"/><span>Connecting…</span></div>
            :"error" in stream?<span className="error">{stream.error}</span>
            :stream.kind==="hls"?<HlsVideo src={stream.stream_url} token={token}/>
            :stream.browser_playable&&stream.kind==="link"?<a href={stream.stream_url} target="_blank" rel="noreferrer">Open stream</a>
            :<span className="muted stream-message">Live preview not available in the browser for this camera{stream.kind==="webrtc"?" (WebRTC client required)":""}. Use snapshot or the edge connector&apos;s own viewer.</span>}
        </div>
        <div className="camera-meta"><div><h3>{camera.name}</h3><p>{camera.type} — Connected</p></div></div>
      </article>;
    })}
  </section>;
}

function LoadingDashboard(){
  return <div aria-busy="true" aria-label="Loading dashboard">
    <div className="skeleton skeleton-hero"/>
    <div className="skeleton-grid"><div className="skeleton skeleton-camera"/><div className="skeleton skeleton-camera"/></div>
  </div>;
}

const REQUEST_TIMEOUT_MS=15_000;

class ApiRequestError extends Error{
  constructor(readonly status:number){
    super(`HTTP ${status}`);
  }
}

async function json<T>(url:string,token:string|null,timeoutMs=REQUEST_TIMEOUT_MS):Promise<T>{
  const controller=new AbortController();
  const timer=setTimeout(()=>controller.abort(),timeoutMs);
  try{
    const response=await fetch(url,{
      signal:controller.signal,
      headers:{...(token?{Authorization:`Bearer ${token}`}:{ }),"X-HomeCam-Request":"1"},
      credentials:"include",
    });
    if(!response.ok) throw new ApiRequestError(response.status);
    return await response.json() as T;
  }finally{
    clearTimeout(timer);
  }
}

function tabFrom(value:string|null):Tab{
  return TABS.find(tab=>tab.toLowerCase()===value?.toLowerCase())||"Overview";
}

export default function Dashboard(){
  const router=useRouter();
  const searchParams=useSearchParams();
  const [tab,setTab]=useState<Tab>(()=>tabFrom(searchParams.get("tab")));
  const [cameras,setCameras]=useState<Camera[]>([]);
  const [events,setEvents]=useState<EventItem[]>([]);
  const [persons,setPersons]=useState<Person[]>([]);
  const [loading,setLoading]=useState(true);
  const [error,setError]=useState("");
  const [token,setToken]=useState<string|null>(null);
  const [authenticated,setAuthenticated]=useState(false);
  const [authReady,setAuthReady]=useState(false);
  const [loginEmail,setLoginEmail]=useState("");
  const [loginPassword,setLoginPassword]=useState("");
  const [loginError,setLoginError]=useState("");
  const [loginBusy,setLoginBusy]=useState(false);
  const [showRegistration,setShowRegistration]=useState(false);
  const [bootstrapSecret,setBootstrapSecret]=useState("");
  const [newEventIds,setNewEventIds]=useState<Set<string>>(new Set());
  const [googleEnabled,setGoogleEnabled]=useState(false);
  const [googleLinked,setGoogleLinked]=useState(false);
  const [googleMessage,setGoogleMessage]=useState<{error:boolean;text:string}|null>(()=>googleNotice(searchParams));
  const [linkBusy,setLinkBusy]=useState(false);
  const tabParam=searchParams.get("tab");
  const hasGoogleParams=searchParams.has("google_error")||searchParams.has("google");

  useEffect(()=>{
    // Drop the one-shot callback result from the address bar.
    if(hasGoogleParams) router.replace(tabParam?`/?tab=${encodeURIComponent(tabParam)}`:"/",{scroll:false});
  },[hasGoogleParams,router,tabParam]);

  useEffect(()=>{
    const controller=new AbortController();
    fetch(`${API}/api/v1/auth/google/status`,{cache:"no-store",signal:controller.signal})
      .then(response=>response.ok?response.json():null)
      .then((body:{enabled?:boolean}|null)=>{if(!controller.signal.aborted) setGoogleEnabled(body?.enabled===true);})
      .catch(()=>{});
    return ()=>controller.abort();
  },[]);

  useEffect(()=>setTab(tabFrom(tabParam)),[tabParam]);

  useEffect(()=>{
    const controller=new AbortController();
    fetch(`${API}/api/v1/auth/me`,{
      credentials:"include",
      cache:"no-store",
      signal:controller.signal,
    }).then(response=>{
      if(controller.signal.aborted) return;
      if(response.ok){
        setAuthenticated(true);
        void response.json().then((body:{google_linked?:boolean})=>setGoogleLinked(body?.google_linked===true)).catch(()=>{});
      }
      else if(response.status===403) setLoginError("This account is disabled or awaiting approval.");
      else if(response.status!==401) setLoginError("HomeCam could not verify the current session.");
    }).catch(()=>{
      if(!controller.signal.aborted) setLoginError("HomeCam could not reach the API. Try again.");
    }).finally(()=>{
      if(!controller.signal.aborted) setAuthReady(true);
    });
    return ()=>controller.abort();
  },[]);

  const load=useCallback(async()=>{
    if(!authenticated) return;
    setLoading(true);
    setError("");
    // Each source loads independently: the slow NVR-backed camera call must not
    // take down events and people when it times out or fails.
    const [cameraResult,eventResult,personResult]=await Promise.allSettled([
      json<Camera[]>(`${API}/api/v1/cameras`,token),
      json<EventItem[]>(`${API}/api/v1/events?limit=${EVENT_LIMIT}`,token),
      json<{persons?:Person[]}>(`${API}/api/v1/persons`,token),
    ]);
    if([cameraResult,eventResult,personResult].some(result=>
      result.status==="rejected"&&result.reason instanceof ApiRequestError&&result.reason.status===401
    )){
      setToken(null);
      setAuthenticated(false);
      setCameras([]);
      setEvents([]);
      setPersons([]);
      setLoading(false);
      return;
    }
    const degraded:string[]=[];
    if(cameraResult.status==="fulfilled") setCameras(cameraResult.value);
    else degraded.push("Camera status is unavailable.");
    if(eventResult.status==="fulfilled") setEvents(eventResult.value.slice(0,EVENT_LIMIT));
    else degraded.push("Recent events are unavailable.");
    if(personResult.status==="fulfilled") setPersons(personResult.value.persons||[]);
    else degraded.push("People are unavailable.");
    if(degraded.length===3) setError("HomeCam could not reach the local API. Check the service and try again.");
    else if(degraded.length) setError(degraded.join(" "));
    setLoading(false);
  },[authenticated,token]);

  const refreshEvents=useCallback(async()=>{
    if(!authenticated) return;
    const [eventResult,personResult]=await Promise.allSettled([
      json<EventItem[]>(`${API}/api/v1/events?limit=${EVENT_LIMIT}`,token),
      json<{persons?:Person[]}>(`${API}/api/v1/persons`,token),
    ]);
    if([eventResult,personResult].some(result=>
      result.status==="rejected"&&result.reason instanceof ApiRequestError&&result.reason.status===401
    )){
      setToken(null);
      setAuthenticated(false);
      setCameras([]);
      setEvents([]);
      setPersons([]);
      return;
    }
    if(eventResult.status==="fulfilled") setEvents(eventResult.value.slice(0,EVENT_LIMIT));
    if(personResult.status==="fulfilled") setPersons(personResult.value.persons||[]);
    if(eventResult.status==="rejected"||personResult.status==="rejected") setError("Events could not be refreshed. Try again.");
  },[authenticated,token]);

  useEffect(()=>{if(authenticated) load();},[authenticated,load]);
  useEffect(()=>{
    if(!authenticated) return;
    const controller=new AbortController();
    void consumeSse(`${API}/api/v1/ws`,token,controller.signal,(type,data)=>{
      if(type!=="event.created"&&type!=="event.updated") return;
      try{
        const next=JSON.parse(data) as EventItem;
        setEvents(current=>type==="event.updated"
          ? current.map(event=>event.id===next.id?{...event,...next}:event)
          : [next,...current.filter(event=>event.id!==next.id)].slice(0,EVENT_LIMIT));
        if(type==="event.created") setNewEventIds(current=>new Set(current).add(next.id));
      }catch{
        setError("A live event update could not be read. Existing events are still available.");
      }
    }).catch(error=>{
      if(controller.signal.aborted) return;
      if(error instanceof SseResponseError&&error.status===401){
        setToken(null);
        setAuthenticated(false);
        setCameras([]);
        setEvents([]);
        setPersons([]);
      }
      else setError("Live event updates are temporarily unavailable.");
    });
    return ()=>controller.abort();
  },[authenticated,token]);

  const signIn=async(event:FormEvent)=>{
    event.preventDefault();
    setLoginBusy(true);
    setLoginError("");
    try{
      const response=await fetch(`${API}/api/v1/auth/login`,{
        method:"POST",
        headers:{"Content-Type":"application/json"},
        credentials:"include",
        cache:"no-store",
        body:JSON.stringify({email:loginEmail,password:loginPassword}),
      });
      if(!response.ok){
        setLoginError(response.status===403
          ?"This account is disabled or awaiting approval."
          :"Invalid email or password.");
        return;
      }
      const body=await response.json() as {access_token?:string;user?:{google_linked?:boolean}};
      if(!body.access_token){
        setLoginError("Sign-in did not return a session.");
        return;
      }
      setToken(body.access_token);
      setGoogleLinked(body.user?.google_linked===true);
      setAuthenticated(true);
      setLoginPassword("");
    }catch{
      setLoginError("HomeCam could not reach the local API. Try again.");
    }finally{
      setLoginBusy(false);
    }
  };

  const createAccount=async(event:FormEvent)=>{
    event.preventDefault();
    setLoginBusy(true);
    setLoginError("");
    try{
      const registration=await fetch(`${API}/api/v1/auth/register`,{
        method:"POST",
        headers:{
          "Content-Type":"application/json",
          "X-HomeCam-Bootstrap-Secret":bootstrapSecret,
          "X-HomeCam-Request":"1",
        },
        credentials:"include",
        cache:"no-store",
        body:JSON.stringify({email:loginEmail,password:loginPassword}),
      });
      if(!registration.ok){
        setLoginError("Account setup was not accepted. Check the setup secret or sign in with the existing account.");
        return;
      }
      const login=await fetch(`${API}/api/v1/auth/login`,{
        method:"POST",
        headers:{"Content-Type":"application/json"},
        credentials:"include",
        cache:"no-store",
        body:JSON.stringify({email:loginEmail,password:loginPassword}),
      });
      if(!login.ok){
        setLoginError("The account was created but automatic sign-in failed. Sign in with the new account.");
        setShowRegistration(false);
        setLoginPassword("");
        setBootstrapSecret("");
        return;
      }
      const body=await login.json() as {access_token?:string};
      if(!body.access_token){
        setLoginError("The account was created but sign-in did not return a session.");
        return;
      }
      setToken(body.access_token);
      setAuthenticated(true);
      setLoginPassword("");
      setBootstrapSecret("");
    }catch{
      setLoginError("HomeCam could not reach the API. Try again.");
    }finally{
      setLoginBusy(false);
    }
  };

  const signOut=useCallback(async()=>{
    try{
      const response=await fetch(`${API}/api/v1/auth/logout`,{
        method:"POST",
        headers:{...(token?{Authorization:`Bearer ${token}`}:{ }),"X-HomeCam-Request":"1"},
        credentials:"include",
      });
      if(!response.ok&&response.status!==401){
        setError("Could not end the current session. Try again.");
        return;
      }
    }catch{
      setError("Could not reach HomeCam to end the current session.");
      return;
    }
    setToken(null);
    setAuthenticated(false);
    setCameras([]);
    setEvents([]);
    setPersons([]);
  },[token]);

  const linkGoogle=async()=>{
    setLinkBusy(true);
    setGoogleMessage(null);
    try{
      const response=await fetch(`${API}/api/v1/auth/google/link`,{
        method:"POST",
        headers:{...(token?{Authorization:`Bearer ${token}`}:{}),"X-HomeCam-Request":"1"},
        credentials:"include",
        cache:"no-store",
      });
      const body=response.ok?await response.json() as {url?:string}:null;
      if(!body?.url||!body.url.startsWith("/api/v1/auth/google/start?")){
        setGoogleMessage({error:true,text:"Google linking is unavailable right now."});
        return;
      }
      window.location.assign(`${API}${body.url}`);
    }catch{
      setGoogleMessage({error:true,text:"HomeCam could not reach the API. Try again."});
    }finally{
      setLinkBusy(false);
    }
  };

  const activeCameras=useMemo(()=>cameras.filter(camera=>camera.online),[cameras]);
  const offlineCount=cameras.length-activeCameras.length;
  const status=homeStatus(cameras,events);
  const selectTab=(next:Tab)=>{
    setTab(next);
    const query=next==="Overview"?"":`?tab=${next.toLowerCase()}`;
    router.push(`/${query}`,{scroll:false});
  };
  const statusTone=error||offlineCount>0||(!loading&&cameras.length===0)?"attention":"healthy";

  if(!authReady) return <LoadingDashboard/>;

  if(!authenticated){
    return <main>
      <header>
        <div><span className="eyebrow">LOCAL-FIRST SECURITY</span><h1>HomeCam <em>AI</em></h1></div>
      </header>
      <section className="panel admin-panel" aria-labelledby="signin-title">
        <h2 id="signin-title">{showRegistration?"Create the first account":"Sign in to HomeCam"}</h2>
        <p className="muted">{showRegistration
          ?"Initial setup requires the one-time secret supplied by the system owner."
          :"Sign in to view cameras, events, people, and security controls."}</p>
        <form className="admin-form" onSubmit={showRegistration?createAccount:signIn}>
          <label>Email<input type="email" autoComplete="username" required value={loginEmail}
            onChange={event=>setLoginEmail(event.target.value)}/></label>
          <label>Password<input type="password" autoComplete={showRegistration?"new-password":"current-password"} required value={loginPassword}
            onChange={event=>setLoginPassword(event.target.value)}/></label>
          {showRegistration&&<label>One-time setup secret<input type="password" autoComplete="off" required
            value={bootstrapSecret} onChange={event=>setBootstrapSecret(event.target.value)}/></label>}
          <div className="admin-actions"><button type="submit" disabled={loginBusy}>
            {loginBusy?(showRegistration?"Creating account…":"Signing in…"):(showRegistration?"Create account":"Sign in")}
          </button>
          <button type="button" className="text-button" disabled={loginBusy}
            onClick={()=>{setShowRegistration(value=>!value);setLoginError("");}}>
            {showRegistration?"Back to sign in":"Create first account"}
          </button></div>
          {loginError&&<p className="error" role="alert">{loginError}</p>}
          {googleMessage&&<p className={googleMessage.error?"error":"success"} role={googleMessage.error?"alert":"status"}>{googleMessage.text}</p>}
        </form>
        {googleEnabled&&!showRegistration&&<div className="admin-actions google-signin">
          <a className="button" href={`${API}/api/v1/auth/google/start`}>Continue with Google</a>
        </div>}
      </section>
    </main>;
  }

  return <main>
    <header>
      <div><span className="eyebrow">LOCAL-FIRST SECURITY</span><h1>HomeCam <em>AI</em></h1></div>
      <button type="button" className="text-button" onClick={signOut}>Sign out</button>
      <span className={`status status-${statusTone}`} role="status">
        <i aria-hidden="true"/>{loading?"Checking system":statusTone==="healthy"?"System operational":"Attention needed"}
      </span>
    </header>

    <nav className="tabs" role="tablist" aria-label="HomeCam sections">
      {TABS.map(item=><button type="button" role="tab" id={`tab-${item.toLowerCase()}`}
        aria-selected={tab===item} aria-controls={`panel-${item.toLowerCase()}`}
        tabIndex={tab===item?0:-1} className={tab===item?"active":""}
        onClick={()=>selectTab(item)} key={item}>
        {item}{item==="Events"&&newEventIds.size>0&&<span className="tab-badge" aria-label={`${newEventIds.size} new`}>{newEventIds.size}</span>}
      </button>)}
    </nav>

    <section className="hero" aria-live="polite">
      <div className="hero-copy">
        <span className="eyebrow">{new Date().toLocaleDateString(undefined,{weekday:"long",month:"long",day:"numeric"})}</span>
        <h2>{greetingForHour(new Date().getHours())}</h2>
        <p>{loading?"Checking cameras and recent activity…":error?"Live status is temporarily unavailable.":status}</p>
      </div>
      <div className="metric"><strong>{loading?"—":activeCameras.length}</strong><span>CAMERAS ONLINE</span></div>
      <div className="metric"><strong>{loading?"—":events.length}</strong><span>LATEST EVENTS LOADED</span></div>
    </section>

    {loading?<LoadingDashboard/>:error&&cameras.length===0&&events.length===0
      ?<section className="panel error-state" role="alert">
        <span className="error-icon" aria-hidden="true">!</span>
        <div><h3>HomeCam is not responding</h3><p>{error}</p></div>
        <button type="button" onClick={load}>Retry</button>
      </section>
      :<>
        {error&&<div className="inline-error" role="alert"><span>{error}</span><button type="button" onClick={load}>Retry</button></div>}
        {tab==="Overview"&&<div id="panel-overview" role="tabpanel" aria-labelledby="tab-overview">
          <CameraGrid cameras={activeCameras}/>
          <section className="panel activity-panel">
            <div className="panel-heading"><div><span className="eyebrow">AT A GLANCE</span><h3>Recent activity</h3></div>
              <button type="button" className="text-button" onClick={()=>selectTab("Events")}>View all</button>
            </div>
            {events.slice(0,3).map(event=><article className="event" key={event.id}>
              <b>{event.type}</b><span>{event.description}</span><small>{new Date(event.start_time).toLocaleTimeString([],{hour:"2-digit",minute:"2-digit"})}</small>
            </article>)}
            {!events.length&&<div className="empty-state compact"><strong>Nothing to review</strong><p>New activity will appear here automatically.</p></div>}
          </section>
        </div>}
        {tab==="Live"&&<LiveView cameras={activeCameras} token={token}/>}
        {tab==="Events"&&<EventsPanel events={events} persons={persons} cameras={cameras}
          token={token} useSessionCookie newEventIds={newEventIds} onChanged={refreshEvents} onAcknowledgeNew={()=>setNewEventIds(new Set())}/>}
        {tab==="People"&&<div id="panel-people" role="tabpanel" aria-labelledby="tab-people"><PeoplePanel token={token} useSessionCookie/></div>}
        {tab==="Security"&&<div id="panel-security" role="tabpanel" aria-labelledby="tab-security"><SecurityPanel cameras={cameras} authToken={token} authenticated={authenticated} onUnauthorized={signOut}/></div>}
        {tab==="System"&&<section className="panel system-panel" id="panel-system" role="tabpanel" aria-labelledby="tab-system">
          <div className="panel-heading"><div><span className="eyebrow">SYSTEM HEALTH</span><h3>Camera connections</h3></div></div>
          <p className={offlineCount?"error":"success"}>{status}</p>
          <dl className="health-grid">
            <div><dt>Configured</dt><dd>{cameras.length}</dd></div>
            <div><dt>Online</dt><dd>{activeCameras.length}</dd></div>
            <div><dt>Needs attention</dt><dd>{offlineCount}</dd></div>
          </dl>
          {(googleEnabled||googleLinked||googleMessage)&&<div aria-labelledby="account-title">
            <h3 id="account-title">Account</h3>
            <p className="muted">{googleLinked?"Google sign-in is linked to this account.":"Google sign-in is not linked to this account."}</p>
            {googleEnabled&&!googleLinked&&<button type="button" disabled={linkBusy} onClick={linkGoogle}>
              {linkBusy?"Opening Google…":"Link Google account"}</button>}
            {googleMessage&&<p className={googleMessage.error?"error":"success"} role={googleMessage.error?"alert":"status"}>{googleMessage.text}</p>}
          </div>}
        </section>}
        {tab==="Settings"&&<div id="panel-settings" role="tabpanel" aria-labelledby="tab-settings"><AdminPanel authToken={token} authenticated={authenticated} onUnauthorized={signOut}/></div>}
      </>}
  </main>;
}

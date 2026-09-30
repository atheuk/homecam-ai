"use client";

import {useCallback,useEffect,useMemo,useState} from "react";
import {useRouter,useSearchParams} from "next/navigation";
import AdminPanel from "./AdminPanel";
import EventsPanel from "./EventsPanel";
import SecurityPanel from "./SecurityPanel";
import {DigestCard,SearchCard} from "./Insights";
import {HlsVideo} from "./Player";
import {EventCard,PeoplePanel,type EventItem,type Person} from "./People";

const API=process.env.NEXT_PUBLIC_API_URL||"http://localhost:8000";
const EVENT_LIMIT=50;
const ACTIVITY_WINDOW_MS=10*60*1000;
const TABS=["Overview","Live","Events","People","Security","System","Settings"] as const;
type Tab=typeof TABS[number];

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
export function LiveView({cameras}:{cameras:Camera[]}){
  const [streams,setStreams]=useState<Record<string,LiveStream>>({});
  useEffect(()=>{
    let cancelled=false;
    cameras.forEach(camera=>{
      fetch(`${API}/api/v1/cameras/${camera.id}/live`).then(async response=>{
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
  },[cameras]);

  if(!cameras.length) return <div className="empty-state"><strong>No connected cameras</strong><p>Live streams will appear when a camera comes online.</p></div>;
  return <section className="grid" id="panel-live" role="tabpanel" aria-labelledby="tab-live">
    {cameras.map(camera=>{
      const stream=streams[camera.id];
      return <article className="camera" key={camera.id}>
        <div className="camera-art stream-art">
          {!stream?<div className="stream-loading"><span className="spinner"/><span>Connecting…</span></div>
            :"error" in stream?<span className="error">{stream.error}</span>
            :stream.kind==="hls"?<HlsVideo src={stream.stream_url}/>
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

async function json<T>(url:string,timeoutMs=REQUEST_TIMEOUT_MS):Promise<T>{
  const controller=new AbortController();
  const timer=setTimeout(()=>controller.abort(),timeoutMs);
  try{
    const response=await fetch(url,{signal:controller.signal});
    if(!response.ok) throw new Error(`HTTP ${response.status}`);
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
  const [newEventIds,setNewEventIds]=useState<Set<string>>(new Set());
  const tabParam=searchParams.get("tab");

  useEffect(()=>setTab(tabFrom(tabParam)),[tabParam]);

  const load=useCallback(async()=>{
    setLoading(true);
    setError("");
    // Each source loads independently: the slow NVR-backed camera call must not
    // take down events and people when it times out or fails.
    const [cameraResult,eventResult,personResult]=await Promise.allSettled([
      json<Camera[]>(`${API}/api/v1/cameras`),
      json<EventItem[]>(`${API}/api/v1/events?limit=${EVENT_LIMIT}`),
      json<{persons?:Person[]}>(`${API}/api/v1/persons`),
    ]);
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
  },[]);

  const refreshEvents=useCallback(async()=>{
    const [eventResult,personResult]=await Promise.allSettled([
      json<EventItem[]>(`${API}/api/v1/events?limit=${EVENT_LIMIT}`),
      json<{persons?:Person[]}>(`${API}/api/v1/persons`),
    ]);
    if(eventResult.status==="fulfilled") setEvents(eventResult.value.slice(0,EVENT_LIMIT));
    if(personResult.status==="fulfilled") setPersons(personResult.value.persons||[]);
    if(eventResult.status==="rejected"||personResult.status==="rejected") setError("Events could not be refreshed. Try again.");
  },[]);

  useEffect(()=>{load();},[load]);
  useEffect(()=>{
    const source=new EventSource(`${API}/api/v1/ws`);
    const receive=(message:Event)=>{
      try{
        const next=JSON.parse((message as MessageEvent).data) as EventItem;
        setEvents(current=>[next,...current.filter(event=>event.id!==next.id)].slice(0,EVENT_LIMIT));
        setNewEventIds(current=>new Set(current).add(next.id));
      }catch{
        setError("A live event update could not be read. Existing events are still available.");
      }
    };
    source.addEventListener("event.created",receive);
    return ()=>source.close();
  },[]);

  const activeCameras=useMemo(()=>cameras.filter(camera=>camera.online),[cameras]);
  const offlineCount=cameras.length-activeCameras.length;
  const status=homeStatus(cameras,events);
  const selectTab=(next:Tab)=>{
    setTab(next);
    const query=next==="Overview"?"":`?tab=${next.toLowerCase()}`;
    router.push(`/${query}`,{scroll:false});
  };
  const statusTone=error||offlineCount>0||(!loading&&cameras.length===0)?"attention":"healthy";

  return <main>
    <header>
      <div><span className="eyebrow">LOCAL-FIRST SECURITY</span><h1>HomeCam <em>AI</em></h1></div>
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
          <SearchCard/>
          <DigestCard/>
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
        {tab==="Live"&&<LiveView cameras={activeCameras}/>}
        {tab==="Events"&&<EventsPanel events={events} persons={persons} cameras={cameras}
          newEventIds={newEventIds} onChanged={refreshEvents} onAcknowledgeNew={()=>setNewEventIds(new Set())}/>}
        {tab==="People"&&<div id="panel-people" role="tabpanel" aria-labelledby="tab-people"><PeoplePanel/></div>}
        {tab==="Security"&&<div id="panel-security" role="tabpanel" aria-labelledby="tab-security"><SecurityPanel cameras={cameras}/></div>}
        {tab==="System"&&<section className="panel system-panel" id="panel-system" role="tabpanel" aria-labelledby="tab-system">
          <div className="panel-heading"><div><span className="eyebrow">SYSTEM HEALTH</span><h3>Camera connections</h3></div></div>
          <p className={offlineCount?"error":"success"}>{status}</p>
          <dl className="health-grid">
            <div><dt>Configured</dt><dd>{cameras.length}</dd></div>
            <div><dt>Online</dt><dd>{activeCameras.length}</dd></div>
            <div><dt>Needs attention</dt><dd>{offlineCount}</dd></div>
          </dl>
        </section>}
        {tab==="Settings"&&<div id="panel-settings" role="tabpanel" aria-labelledby="tab-settings"><AdminPanel/></div>}
      </>}
  </main>;
}

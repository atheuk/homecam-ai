"use client";
import {useEffect,useState} from "react";
import AdminPanel from "./AdminPanel";
const API=process.env.NEXT_PUBLIC_API_URL||"http://localhost:8000";
type Camera={id:string;name:string;type:string;online:boolean;battery_level?:number}; type Event={id:string;camera_id:string;type:string;description:string;start_time:string};
type LiveStream={kind:string;browser_playable:boolean;stream_url:string}|{error:string};

/** Live tab: fetches a browser-safe stream descriptor per camera from
 * GET /cameras/{id}/live and renders it. Never renders raw RTSP URLs or
 * credentials — only the `kind`/`stream_url` the API already vetted (see
 * app/api/routes.py:_classify_stream_url), with a link/fallback for
 * kinds this simple viewer cannot embed directly (e.g. WebRTC signaling
 * endpoints, which need a dedicated client). */
function LiveView({cams}:{cams:Camera[]}){
  const [streams,setStreams]=useState<Record<string,LiveStream>>({});
  useEffect(()=>{
    let cancelled=false;
    cams.forEach(c=>{
      fetch(`${API}/api/v1/cameras/${c.id}/live`).then(async r=>{
        if(!r.ok){if(!cancelled)setStreams(s=>({...s,[c.id]:{error:`Stream unavailable (HTTP ${r.status}).`}}));return;}
        const body=await r.json();
        if(!cancelled)setStreams(s=>({...s,[c.id]:{kind:body.kind,browser_playable:body.browser_playable,stream_url:body.stream_url}}));
      }).catch(()=>{if(!cancelled)setStreams(s=>({...s,[c.id]:{error:"Stream unavailable."}}));});
    });
    return ()=>{cancelled=true};
  },[cams]);

  if(!cams.length) return <p className="muted">No cameras discovered yet.</p>;

  return <section className="grid">{cams.map(c=>{
    const stream=streams[c.id];
    return <article className="camera" key={c.id}>
      <div className="camera-art">
        {!stream ? <span className="muted">Loading stream…</span>
          : "error" in stream ? <span className="error">{stream.error}</span>
          : stream.kind==="hls" ? <video controls muted playsInline src={stream.stream_url} style={{width:"100%"}}/>
          : stream.browser_playable && stream.kind==="link" ? <a href={stream.stream_url} target="_blank" rel="noreferrer">Open stream</a>
          : <span className="muted">Live preview not available in the browser for this camera{stream.kind==="webrtc"?" (WebRTC client required)":""}. Use snapshot or the edge connector&apos;s own viewer.</span>}
      </div>
      <div className="camera-meta"><div><h3>{c.name}</h3><p>{c.type} — {c.online?"Connected":"Unavailable"}</p></div></div>
    </article>;
  })}</section>;
}

export default function Home(){const [cams,setCams]=useState<Camera[]>([]);const [events,setEvents]=useState<Event[]>([]);const [tab,setTab]=useState("Overview");
useEffect(()=>{Promise.all([fetch(`${API}/api/v1/cameras`).then(r=>r.json()),fetch(`${API}/api/v1/events`).then(r=>r.json())]).then(([c,e])=>{setCams(c);setEvents(e)});const es=new EventSource(`${API}/api/v1/ws`);es.addEventListener("event.created",e=>setEvents(x=>[JSON.parse((e as MessageEvent).data),...x]));return()=>es.close()},[]);
return <main><header><div><span className="eyebrow">LOCAL-FIRST SECURITY</span><h1>HomeCam <em>AI</em></h1></div><span className="status"><i/> System operational</span></header><nav>{["Overview","Live","Events","System","Settings"].map(x=><button className={tab===x?"active":""} onClick={()=>setTab(x)} key={x}>{x}</button>)}</nav><section className="hero"><div><span className="eyebrow">{new Date().toLocaleDateString()}</span><h2>Good evening.</h2><p>Your home is quiet. All cameras are connected.</p></div><div className="metric"><strong>{cams.length}</strong><span>CAMERAS ONLINE</span></div><div className="metric"><strong>{events.length}</strong><span>RECENT EVENTS</span></div></section>{tab==="Live"?<LiveView cams={cams}/>:tab==="Events"?<section className="panel"><h3>Recent events</h3>{events.length?events.map(e=><article className="event" key={e.id}><b>{e.type.toUpperCase()}</b><span>{e.description}</span><small>{new Date(e.start_time).toLocaleTimeString()}</small></article>):<p className="muted">No events yet. Use POST /api/v1/mock/events to simulate one.</p>}</section>:tab==="Settings"?<AdminPanel/>:<><section className="grid">{cams.map(c=><article className="camera" key={c.id}><div className="camera-art"><span>{c.type==="doorbell"?"?":"?"}</span><label>{c.online?"LIVE":"OFFLINE"}</label></div><div className="camera-meta"><div><h3>{c.name}</h3><p>{c.type} ? {c.online?"Connected":"Unavailable"}</p></div>{c.battery_level&&<span className="battery">{c.battery_level}%</span>}</div></article>)}</section><section className="panel"><h3>Activity stream</h3>{events.slice(0,3).map(e=><article className="event" key={e.id}><b>{e.type}</b><span>{e.description}</span><small>{new Date(e.start_time).toLocaleTimeString()}</small></article>)}{!events.length&&<p className="muted">No activity detected.</p>}</section></>}</main>}

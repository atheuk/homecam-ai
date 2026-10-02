"use client";
/** Short video clip attached to an event, played from an authenticated blob.
 *
 * Every status is shown honestly: a clip that is still being recorded, one
 * the camera cannot provide, one skipped by the storage budget and one that
 * expired are all different things to the owner.
 */
import {useEffect,useRef,useState} from "react";

const API=process.env.NEXT_PUBLIC_API_URL||"http://localhost:8000";

export type ClipStatus="none"|"pending"|"ready"|"unavailable"|"unsupported"|"skipped"|"expired";

export type EventClipInfo={
  status:ClipStatus;
  url:string|null;
  reason?:string|null;
  source?:"stream"|"edge"|"incident"|string|null;
  incident_id?:string|null;
  duration_seconds?:number|null;
  pre_roll_seconds?:number|null;
  codec?:string|null;
  width?:number|null;
  height?:number|null;
  size_bytes?:number|null;
};

function authHeaders(token?:string|null):Record<string,string>{
  return {"X-HomeCam-Request":"1",...(token?{Authorization:`Bearer ${token}`}:{})};
}

/** "12.4 s clip · starts 6 s before the event" in plain words. */
export function clipFacts(clip:EventClipInfo):string{
  const parts:string[]=[];
  if(clip.duration_seconds!=null) parts.push(`${clip.duration_seconds.toFixed(1)} s clip`);
  if(clip.pre_roll_seconds!=null){
    parts.push(clip.pre_roll_seconds>0.5
      ? `starts ${Math.round(clip.pre_roll_seconds)} s before the event`
      : "starts when the camera woke (no earlier footage)");
  }
  if(clip.height) parts.push(`${clip.height}p`);
  return parts.join(" · ");
}

const STATUS_TEXT:Partial<Record<ClipStatus,string>>={
  pending:"Recording clip…",
  unavailable:"No clip for this event",
  unsupported:"This camera cannot record event clips",
  skipped:"Clip skipped: daily or storage limit reached",
  expired:"Clip expired",
};

export function hasPlayableClip(clip?:EventClipInfo|null){
  return clip?.status==="ready"&&Boolean(clip.url);
}

export default function EventClip({eventId,clip,token}:{eventId:string;clip?:EventClipInfo|null;token?:string|null}){
  const [objectUrl,setObjectUrl]=useState<string|null>(null);
  const [loading,setLoading]=useState(false);
  const [downloading,setDownloading]=useState(false);
  const [error,setError]=useState<string|null>(null);
  const objectUrlRef=useRef<string|null>(null);
  const requestRef=useRef<AbortController|null>(null);

  useEffect(()=>()=>{
    requestRef.current?.abort();
    if(objectUrlRef.current) URL.revokeObjectURL(objectUrlRef.current);
  },[]);

  if(!clip||clip.status==="none") return null;

  function resolve(download=false):URL{
    if(!clip?.url) throw new Error("Clip URL is missing.");
    const apiUrl=new URL(API,window.location.href);
    const url=new URL(clip.url,apiUrl);
    if(url.origin!==apiUrl.origin) throw new Error("Clip URL is not trusted.");
    if(download) url.searchParams.set("download","true");
    return url;
  }

  async function play(){
    if(loading||objectUrl) return;
    setLoading(true);setError(null);
    const controller=new AbortController();
    requestRef.current=controller;
    try{
      const response=await fetch(resolve().href,{headers:authHeaders(token),credentials:"include",signal:controller.signal});
      if(!response.ok) throw new Error(`HTTP ${response.status}`);
      const blob=await response.blob();
      if(controller.signal.aborted) return;
      const url=URL.createObjectURL(blob);
      objectUrlRef.current=url;
      setObjectUrl(url);
    }catch(cause){
      if(!(cause instanceof Error&&cause.name==="AbortError")) setError("Could not load this clip. Please try again.");
    }finally{
      if(requestRef.current===controller){requestRef.current=null;setLoading(false);}
    }
  }

  async function download(){
    if(downloading) return;
    setDownloading(true);setError(null);
    try{
      const response=await fetch(resolve(true).href,{headers:authHeaders(token),credentials:"include"});
      if(!response.ok) throw new Error(`HTTP ${response.status}`);
      const url=URL.createObjectURL(await response.blob());
      const anchor=document.createElement("a");
      anchor.href=url;
      anchor.download=`event-${eventId}-clip.mp4`;
      anchor.click();
      window.setTimeout(()=>URL.revokeObjectURL(url),0);
    }catch{
      setError("Could not download this clip. Please try again.");
    }finally{
      setDownloading(false);
    }
  }

  if(!hasPlayableClip(clip)){
    const text=STATUS_TEXT[clip.status]||"No clip for this event";
    return <p className="event-clip muted" role="status">
      <span className={`badge clip-${clip.status}`}>{text}</span>
      {clip.reason&&clip.status!=="pending"&&<span> · {clip.reason}</span>}
    </p>;
  }

  const facts=clipFacts(clip);
  return <div className="event-clip">
    <div className="incident-clip-actions">
      {!objectUrl&&<button type="button" disabled={loading} onClick={play}>{loading?"Loading clip…":"▶ Play clip"}</button>}
      <button type="button" disabled={downloading} onClick={download}>{downloading?"Preparing download…":"Download clip"}</button>
      {facts&&<span className="muted clip-facts">{facts}</span>}
    </div>
    {error&&<p className="error" role="alert">{error}</p>}
    {objectUrl&&<video className="incident-clip-video" controls autoPlay muted playsInline preload="metadata"
      aria-label="Event clip" src={objectUrl}/>}
  </div>;
}

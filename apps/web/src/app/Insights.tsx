"use client";

import {useCallback,useEffect,useState} from "react";

const API=process.env.NEXT_PUBLIC_API_URL||"http://localhost:8000";

export type SearchHit={
  id:string;
  camera_id:string;
  type:string;
  description?:string|null;
  start_time:string;
  zone?:string|null;
  tags?:string[]|null;
  notification_priority?:string|null;
  score?:number;
};

export type SearchResponse={
  query:string;
  refused:boolean;
  notice?:string|null;
  blocked_categories?:string[];
  results:SearchHit[];
};

export type DigestResponse={
  date:string;
  summary:string;
  source:string;
  stats:{
    event_count:number;
    incident_count:number;
    by_camera?:Record<string,number>;
    by_type?:Record<string,number>;
    loitering_count?:number;
    unusual_count?:number;
    package_removed_count?:number;
    notable?:{event_id:string;camera_id:string;type:string;at:string;description?:string|null}[];
  };
};

function authHeaders(token:string):Record<string,string>{
  return {Authorization:"Bearer "+token};
}

function localTime(value:string){
  const parsed=new Date(value);
  if(Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString([],{month:"short",day:"numeric",hour:"2-digit",minute:"2-digit"});
}

/** Natural-language event search. Identity questions are refused by the API,
 * and the refusal is shown to the household verbatim rather than hidden. */
export function SearchCard({token}:{token:string}){
  const [query,setQuery]=useState("");
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState("");
  const [response,setResponse]=useState<SearchResponse|null>(null);

  async function run(event:React.FormEvent){
    event.preventDefault();
    const trimmed=query.trim();
    if(!trimmed) return;
    setBusy(true);
    setError("");
    try{
      const r=await fetch(`${API}/api/v1/search?q=${encodeURIComponent(trimmed)}`,{headers:authHeaders(token)});
      if(r.status===503){
        setResponse(null);
        setError("Search is turned off on this system.");
        return;
      }
      if(!r.ok) throw new Error(`HTTP ${r.status}`);
      setResponse(await r.json() as SearchResponse);
    }catch{
      setResponse(null);
      setError("Could not reach the API to search.");
    }finally{
      setBusy(false);
    }
  }

  return <section className="panel search-panel" aria-label="Event search">
    <div className="panel-heading"><div><span className="eyebrow">ASK YOUR CAMERAS</span><h3>Search events</h3></div></div>
    <form onSubmit={run} className="search-form">
      <label className="search-label">
        Describe what you are looking for
        <input value={query} onChange={e=>setQuery(e.target.value)}
          placeholder="package left at the front door last night" aria-label="Search events"/>
      </label>
      <button type="submit" disabled={busy||!query.trim()}>{busy?"Searching…":"Search"}</button>
    </form>
    <p className="muted">Searches what the cameras saw. It cannot tell you who someone is.</p>
    {error&&<p className="error" role="alert">{error}</p>}
    {response?.refused&&<p className="error" role="alert" data-testid="search-refusal">{response.notice}</p>}
    {response&&!response.refused&&response.notice&&<p className="muted" data-testid="search-notice">{response.notice}</p>}
    {response&&!response.refused&&<>
      {response.results.length===0
        ?<div className="empty-state compact"><strong>No matching events</strong><p>Try a different description or a wider time range.</p></div>
        :<ul className="search-results" aria-label="Search results">
          {response.results.map(hit=><li key={hit.id} className="event">
            <b>{hit.type}</b>
            <span>{hit.description||"No description recorded."}</span>
            <small>{hit.camera_id} · {localTime(hit.start_time)}</small>
          </li>)}
        </ul>}
    </>}
  </section>;
}

/** The day-in-review digest (counts, notable items, unusual activity). */
export function DigestCard({token}:{token:string}){
  const [digest,setDigest]=useState<DigestResponse|null>(null);
  const [state,setState]=useState<"loading"|"ready"|"error"|"off">("loading");

  const load=useCallback(async(refresh:boolean)=>{
    setState("loading");
    try{
      const r=await fetch(`${API}/api/v1/digest${refresh?"?refresh=true":""}`,{headers:authHeaders(token)});
      if(r.status===503){setState("off");return;}
      if(!r.ok) throw new Error(`HTTP ${r.status}`);
      const body=await r.json() as DigestResponse;
      if(!body||typeof body.summary!=="string"||!body.stats){setState("error");return;}
      setDigest(body);
      setState("ready");
    }catch{
      setState("error");
    }
  },[token]);

  useEffect(()=>{load(false);},[load]);

  if(state==="off") return null;

  return <section className="panel digest-panel" aria-label="Daily digest">
    <div className="panel-heading">
      <div><span className="eyebrow">DAY IN REVIEW</span><h3>Home digest</h3></div>
      <button type="button" className="text-button" onClick={()=>load(true)} disabled={state==="loading"}>Refresh</button>
    </div>
    {state==="loading"&&<p className="muted">Building today&apos;s digest…</p>}
    {state==="error"&&<p className="muted" data-testid="digest-error">The digest is unavailable right now.</p>}
    {state==="ready"&&digest&&<>
      <p data-testid="digest-summary">{digest.summary}</p>
      <dl className="health-grid">
        <div><dt>Events</dt><dd>{digest.stats.event_count}</dd></div>
        <div><dt>Incidents</dt><dd>{digest.stats.incident_count}</dd></div>
        <div><dt>Unusual</dt><dd>{digest.stats.unusual_count??0}</dd></div>
        <div><dt>Loitering</dt><dd>{digest.stats.loitering_count??0}</dd></div>
      </dl>
      {!!digest.stats.notable?.length&&<ul className="search-results" aria-label="Notable events">
        {digest.stats.notable.map(item=><li key={item.event_id} className="event">
          <b>{item.type}</b>
          <span>{item.description||"No description recorded."}</span>
          <small>{item.camera_id} · {localTime(item.at)}</small>
        </li>)}
      </ul>}
    </>}
  </section>;
}

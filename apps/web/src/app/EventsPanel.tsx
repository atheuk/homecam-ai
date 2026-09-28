"use client";

import {useMemo,useState} from "react";
import {EventCard,type EventItem,type Person} from "./People";

export type CameraSummary={id:string;name:string};

type EventsPanelProps={
  events:EventItem[];
  persons:Person[];
  cameras:CameraSummary[];
  newEventIds:Set<string>;
  onChanged:()=>void;
  onAcknowledgeNew:()=>void;
};

const FILTER_TYPES=["person","vehicle","animal","motion"] as const;

function dayLabel(value:string){
  const date=new Date(value);
  const today=new Date();
  const yesterday=new Date(today);
  yesterday.setDate(today.getDate()-1);
  const key=(item:Date)=>item.toDateString();
  if(key(date)===key(today)) return "Today";
  if(key(date)===key(yesterday)) return "Yesterday";
  return date.toLocaleDateString(undefined,{weekday:"long",month:"long",day:"numeric"});
}

export default function EventsPanel({
  events,persons,cameras,newEventIds,onChanged,onAcknowledgeNew,
}:EventsPanelProps){
  const [type,setType]=useState("all");
  const [cameraId,setCameraId]=useState("all");
  const [namedOnly,setNamedOnly]=useState(false);

  const filtered=useMemo(()=>events.filter(event=>
    (type==="all"||event.type===type)&&
    (cameraId==="all"||event.camera_id===cameraId)&&
    (!namedOnly||Boolean(event.person_display_name))
  ),[events,type,cameraId,namedOnly]);

  const groups=useMemo(()=>{
    const result:{label:string;events:EventItem[]}[]=[];
    for(const event of filtered){
      const label=dayLabel(event.start_time);
      const previous=result[result.length-1];
      if(previous?.label===label) previous.events.push(event);
      else result.push({label,events:[event]});
    }
    return result;
  },[filtered]);

  return <section className="panel events-panel" id="panel-events" role="tabpanel" aria-labelledby="tab-events">
    <div className="panel-heading">
      <div>
        <span className="eyebrow">LATEST 50</span>
        <h3>Events</h3>
      </div>
      <span className="result-count" aria-live="polite">{filtered.length} shown</span>
    </div>

    {newEventIds.size>0&&<button type="button" className="new-events" onClick={onAcknowledgeNew}>
      {newEventIds.size} new {newEventIds.size===1?"event":"events"} received · show latest
    </button>}

    <div className="event-filters" aria-label="Filter events">
      <div className="filter-group" role="group" aria-label="Event type">
        <button type="button" className={type==="all"?"active":""} aria-pressed={type==="all"} onClick={()=>setType("all")}>All</button>
        {FILTER_TYPES.map(value=><button type="button" key={value}
          className={type===value?"active":""} aria-pressed={type===value}
          onClick={()=>setType(value)}>{value}</button>)}
      </div>
      <label className="filter-select">
        <span>Camera</span>
        <select value={cameraId} onChange={event=>setCameraId(event.target.value)}>
          <option value="all">All cameras</option>
          {cameras.map(camera=><option value={camera.id} key={camera.id}>{camera.name}</option>)}
        </select>
      </label>
      <label className="filter-check">
        <input type="checkbox" checked={namedOnly} onChange={event=>setNamedOnly(event.target.checked)}/>
        Named people only
      </label>
    </div>

    {groups.length?groups.map(group=><section className="event-day" key={group.label}>
      <h4>{group.label}</h4>
      {group.events.map(event=><div className={newEventIds.has(event.id)?"event-arrival":""} key={event.id}>
        <EventCard event={event} persons={persons} onChanged={onChanged}/>
      </div>)}
    </section>):events.length
      ?<div className="empty-state"><strong>No matching events</strong><p>Try clearing one or more filters.</p></div>
      :<div className="empty-state"><strong>No events recorded yet</strong><p>New camera activity will appear here automatically.</p></div>}
  </section>;
}

"use client";
/** Person recognition UI: who was seen, what they looked like, and naming them.
 *
 * Two surfaces live here:
 *
 * - {@link EventCard} — an event row that actually shows the stored person
 *   photo plus its plain-language caption, and lets the viewer rate how
 *   usable the photo is and say who it is.
 * - {@link PeoplePanel} — the roster of recognized identities, named or not.
 *
 * Naming is the system's only learning signal (see the API's
 * ``services/persons.py``), so the naming control is deliberately part of
 * the normal event view rather than hidden in a settings screen.
 */
import {useCallback,useEffect,useState} from "react";
import {DetectionBox} from "./DetectionBoxes";
import {ZoomablePhoto} from "./Lightbox";

const API=process.env.NEXT_PUBLIC_API_URL||"http://localhost:8000";

/** What the AI decided an animal was. ``breed`` is deliberately nullable:
 * the model is instructed to return nothing rather than guess a breed it
 * cannot see, and a missing breed must never be filled in by the UI. */
export type AnimalIdentity={
  species:"dog"|"cat"|"bird"|"other";
  breed?:string|null;
  confidence?:number|null;
  description?:string|null;
  common_name?:string|null;scientific_name?:string|null;taxonomic_group?:string|null;
};
export type VehicleIdentity={
  make?:string;model?:string;colour?:string;body_type?:string;year_generation?:string;trim?:string;
  make_confidence?:number;model_confidence?:number;colour_confidence?:number;
  body_type_confidence?:number;year_generation_confidence?:number;trim_confidence?:number;
};
export type SuspiciousAssessment={
  score:number;level:string;reasons:string[];evidence_event_ids:string[];appearance_confidence?:number|null;
};

/** Observable description of a person: what a witness could describe.
 *
 * Deliberately carries no ethnicity or gender. Those are protected
 * attributes the model would be guessing at, and pairing a guess about
 * someone's race with a trust flag is profiling, not home security.
 * Legacy age fields are neither requested nor displayed. */
export type Appearance={
  person_present:boolean;
  age_band?:"child"|"teenager"|"adult"|"older adult"|null;
  age_confidence?:number|null;
  build?:string|null;
  clothing?:string|null;
  carrying?:string|null;
  face_visible?:boolean;
  description?:string|null;
};

/** Whether the household expects this person. Always set by a human. */
export type Trust="unknown"|"trusted"|"watch";

export type EventItem={
  id:string;camera_id:string;type:string;description:string;start_time:string;
  has_photo?:boolean;photo_url?:string|null;full_photo_url?:string|null;photo_caption?:string|null;photo_rating?:number|null;
  photo_boxes?:DetectionBox[]|null;full_photo_boxes?:DetectionBox[]|null;animal?:AnimalIdentity|null;
  appearance?:Appearance|null;
  vehicle?:VehicleIdentity|null;suspicious?:SuspiciousAssessment|null;
  // ``null`` means nobody checked, which is neither confirmation nor doubt.
  photo_verified?:boolean|null;
  photo_capture_status?:"pending"|"captured"|"failed"|null;
  photo_capture_reason?:string|null;photo_fallback?:boolean;
  person_id?:string|null;person_name?:string|null;person_display_name?:string|null;
  person_trust?:Trust|null;
  person_confidence?:number|null;person_confirmed?:boolean;
  tags?:string[]|null;zone?:string|null;
  // What changed, for scene transitions (see apps/api/app/services/scene_state.py).
  scene?:EventScene|null;
};
export type EventScene={
  kind:"vehicle"|"mailbox"|"bin"|string;transition:string;
  zone?:string|null;confidence?:number|null;source?:string|null;parked?:boolean;
  item_type?:string|null;
};

const SCENE_LABELS:Record<string,string>={
  first_seen:"Vehicle seen",arrived:"Arrived",returned:"Returned",moved:"Moved",
  departed:"Left",interaction:"Someone at vehicle",
  mailbox_delivery:"Mail delivered",mailbox_retrieval:"Mail taken out",mailbox_opened:"Mailbox opened",
  mailbox_visit:"Mailbox visit",bin_placed_out:"Bin put out",bin_emptied:"Bin emptied",
};

/** Short, human labels for a scene transition (plus "Parked" once stable). */
export function sceneBadges(event:Pick<EventItem,"scene"|"tags">):string[]{
  const scene=event.scene;
  if(!scene) return [];
  const labels:string[]=[];
  const parcel=scene.item_type==="parcel";
  if(scene.transition==="mailbox_delivery"&&scene.item_type&&scene.item_type!=="unknown"){
    labels.push(parcel?"Parcel delivered":"Mail delivered");
  }else if(scene.transition==="mailbox_retrieval"&&scene.item_type&&scene.item_type!=="unknown"){
    labels.push(parcel?"Parcel taken out":"Mail taken out");
  }else{
    const label=SCENE_LABELS[scene.transition];
    if(label) labels.push(label);
  }
  if(scene.parked||event.tags?.includes("vehicle_parked")) labels.push("Parked");
  return labels;
}

/** Activity bucket used by the events filter. */
export function sceneCategory(event:Pick<EventItem,"scene"|"tags">):"vehicle"|"mailbox"|"bin"|null{
  const kind=event.scene?.kind;
  if(kind==="vehicle"||kind==="mailbox"||kind==="bin") return kind;
  const tags=event.tags||[];
  if(tags.includes("mailbox")||tags.some(tag=>tag.startsWith("mailbox_"))) return "mailbox";
  if(tags.includes("bin_placed_out")||tags.includes("bin_emptied")) return "bin";
  return null;
}
export type Person={
  id:string;name:string|null;display_name:string;named:boolean;notes:string|null;
  trust?:Trust;
  sighting_count:number;reference_samples:number;cover_event_id:string|null;
  photo_url:string|null;first_seen_at:string|null;last_seen_at:string|null;
};
export type Recognition={enabled:boolean;backend:string;semantic:boolean;match_threshold:number};

/** Absolute URL for an API-relative media path. */
export function mediaUrl(path?:string|null){return path?`${API}${path}`:undefined}

function authHeaders(token?:string|null){
  return {
    "Content-Type":"application/json",
    "X-HomeCam-Request":"1",
    ...(token?{Authorization:`Bearer ${token}`}:{})
  };
}

/** 1-5 star control. Rating is how *usable* the photo is, which is what
 * promotes a photo to be a person's cover image. */
export function StarRating({value,onRate,label}:{value?:number|null;onRate:(rating:number|null)=>void;label:string}){
  const stars=[1,2,3,4,5];
  return <div className="rating" role="group" aria-label={label}>
    {stars.map(star=>
      <button key={star} type="button" className={value&&star<=value?"star on":"star"}
        aria-label={`Rate ${star} out of 5`} aria-pressed={!!value&&star<=value}
        onClick={()=>onRate(star===value?null:star)}>★</button>
    )}
    {value?<span className="rating-value">{value}/5</span>:<span className="muted rating-value">Not rated</span>}
  </div>;
}

/** Name-this-person control: pick a known identity, or type a new name. */
function PersonAssign({event,persons,token,onAssigned}:{event:EventItem;persons:Person[];token?:string|null;onAssigned:()=>void}){
  const [name,setName]=useState("");
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState("");

  const submit=async(body:Record<string,string>)=>{
    setBusy(true);setError("");
    try{
      const response=await fetch(`${API}/api/v1/events/${event.id}/person`,{
        method:"POST",headers:authHeaders(token),body:JSON.stringify(body),
        credentials:"include",
      });
      if(!response.ok) throw new Error(`HTTP ${response.status}`);
      setName("");
      onAssigned();
    }catch{setError("Could not save. Try again.");}
    finally{setBusy(false);}
  };

  return <div className="assign">
    <label>
      <span>This is</span>
      <select value={event.person_id||""} disabled={busy}
        aria-label="Assign a known person"
        onChange={e=>{if(e.target.value) submit({person_id:e.target.value});}}>
        <option value="">Select someone…</option>
        {persons.map(person=><option key={person.id} value={person.id}>{person.display_name}</option>)}
      </select>
    </label>
    <label>
      <span>Or add a new name</span>
      <input value={name} disabled={busy} placeholder="e.g. Sarah"
        aria-label="Name this person"
        onChange={e=>setName(e.target.value)}
        onKeyDown={e=>{if(e.key==="Enter"&&name.trim()) submit({name:name.trim()});}}/>
    </label>
    <button type="button" disabled={busy||!name.trim()} onClick={()=>submit({name:name.trim()})}>Save name</button>
    {error&&<span className="error">{error}</span>}
  </div>;
}

/** Plain-language summary of an animal sighting, breed first when known. */
export function describeAnimal(animal:AnimalIdentity){
  if(animal.common_name) return animal.common_name;
  if(animal.breed) return animal.breed;
  return animal.species==="other"?"Unrecognized animal":animal.species.charAt(0).toUpperCase()+animal.species.slice(1);
}

/** Human-readable clothing and carried-item observations, not demographics. */
export function appearanceChips(appearance:Appearance){
  const chips:{key:string;label:string}[]=[];
  if(appearance.build) chips.push({key:"build",label:appearance.build});
  if(appearance.clothing) chips.push({key:"clothing",label:appearance.clothing});
  if(appearance.carrying) chips.push({key:"carrying",label:`Carrying ${appearance.carrying}`});
  if(appearance.face_visible===false) chips.push({key:"face",label:"Face not visible"});
  return chips;
}

const TRUST_LABELS:Record<Trust,string>={unknown:"Not yet known",trusted:"Trusted",watch:"Watch"};

/** Trust badge. Absent for "unknown" so the common case stays quiet. */
export function TrustBadge({trust}:{trust?:Trust|null}){
  if(!trust||trust==="unknown") return null;
  return <span className={`badge trust trust-${trust}`}>{TRUST_LABELS[trust]}</span>;
}

/** One event, with its person photo, caption, rating and identity controls. */
export function EventCard({event,persons,token,useSessionCookie=false,onChanged}:{
  event:EventItem;persons:Person[];token?:string|null;useSessionCookie?:boolean;onChanged:()=>void
}){
  const [rating,setRating]=useState<number|null|undefined>(event.photo_rating);
  useEffect(()=>{setRating(event.photo_rating)},[event.photo_rating]);

  const rate=async(next:number|null)=>{
    setRating(next); // optimistic: the control must feel instant
    try{
      await fetch(`${API}/api/v1/events/${event.id}/rating`,{
        method:"POST",headers:authHeaders(token),body:JSON.stringify({rating:next}),
        credentials:"include",
      });
      onChanged();
    }catch{setRating(event.photo_rating);}
  };

  const identified=event.person_display_name;
  const animal=event.animal;
  // Whatever the subject is called, so its border can be labelled with a
  // name instead of a bare class ("Sarah 93%" / "Border Collie 88%").
  const subject=identified||(animal?describeAnimal(animal):null);
  const chips=event.appearance?appearanceChips(event.appearance):[];
  const badges=sceneBadges(event);
  return <article className="event-card" id={`event-${event.id}`}>
    <div className="event-photo">
      {event.has_photo&&event.photo_url
        ? <ZoomablePhoto src={mediaUrl(event.photo_url)!}
            fullSrc={event.full_photo_url ? mediaUrl(event.full_photo_url) : null}
            loginUrl={mediaUrl("/api/v1/auth/login")}
            accessToken={token}
            useSessionCookie={useSessionCookie}
            requiresAuth
            alt={event.photo_caption||`${event.type} detected`}
            caption={event.photo_caption}
            boxes={event.photo_boxes}
            fullBoxes={event.full_photo_boxes}
            subject={subject}
            title={event.person_display_name||event.description}/>
        : <span className="muted" role="status">{event.photo_capture_status==="pending"
            ? "Capturing photo…"
            : event.photo_capture_reason||"Camera did not return an image"}</span>}
    </div>
    <div className="event-body">
      <div className="event-head">
        <b>{event.type.toUpperCase()}</b>
        <small>{new Date(event.start_time).toLocaleString()}</small>
      </div>
      <p className="event-desc">{event.description}</p>
      {badges.length>0&&<p className="scene" aria-label="What changed">
        {badges.map(label=><span key={label} className="badge scene-badge">{label}</span>)}
        {event.scene?.zone&&<span className="muted"> · {event.scene.zone}</span>}
      </p>}
      {/* The caption is what makes a small crop understandable at a glance. */}
      {event.photo_caption&&<p className="caption">“{event.photo_caption}”</p>}
      {/* Saying so is the honest alternative to silently showing a border
          around a fence post as though it were a person. */}
      {event.photo_verified===false&&<p className="unconfirmed">
        {event.photo_fallback
          ? "Camera frame saved; the event subject is not verified in this photo."
          : "Motion detected, but no person or animal confirmed in the photo."}
      </p>}
      {chips.length>0&&<ul className="appearance" aria-label="What was observed">
        {chips.map(chip=><li key={chip.key} className={`chip chip-${chip.key}`}>{chip.label}</li>)}
      </ul>}
      {animal&&<p className="animal">
        <strong>{describeAnimal(animal)}</strong>
        {animal.scientific_name&&<span className="badge">{animal.scientific_name}</span>}
        {animal.common_name
          ? <span className="badge">{animal.taxonomic_group} · {Math.round((animal.confidence||0)*100)}% sure</span>
          : animal.breed
          ? <span className="badge">{animal.species}{animal.confidence?` · ${Math.round(animal.confidence*100)}% sure`:""}</span>
          : <span className="badge">Species not identifiable</span>}
      </p>}
      {event.vehicle&&<p className="vehicle" aria-label="Vehicle identification">
        {(["colour","make","model","year_generation","body_type","trim"] as const).map(key=>
          event.vehicle?.[key]&&event.vehicle[key]!=="unknown"&&<span key={key} className="badge">
            {event.vehicle[key]}{event.vehicle[`${key}_confidence` as keyof VehicleIdentity]!=null
              ? ` · ${Math.round(Number(event.vehicle[`${key}_confidence` as keyof VehicleIdentity])*100)}%`:""}
          </span>
        )}
      </p>}
      {event.suspicious&&<div className="suspicious" aria-label="Suspicious behaviour assessment">
        <strong>{event.suspicious.level} · score {event.suspicious.score}</strong>
        <ul>{event.suspicious.reasons.map(reason=><li key={reason}>{reason}</li>)}</ul>
        {event.suspicious.evidence_event_ids.length>0&&<p>
          Evidence: {event.suspicious.evidence_event_ids.map(id=><a key={id} href={`#event-${id}`}>{id} </a>)}
        </p>}
      </div>}
      {identified&&<p className="identity">
        <strong>{identified}</strong>
        <TrustBadge trust={event.person_trust}/>
        {event.person_confirmed
          ? <span className="badge confirmed">Confirmed</span>
          : event.person_confidence!=null
            ? <span className="badge">Auto-matched {Math.round(event.person_confidence*100)}%</span>
            : <span className="badge">New face</span>}
      </p>}
      {event.has_photo&&<>
        <StarRating value={rating} label={`Rate the photo for ${event.description}`} onRate={rate}/>
        {!event.photo_fallback&&<PersonAssign event={event} persons={persons} token={token} onAssigned={onChanged}/>}
      </>}
    </div>
  </article>;
}

function PersonRow({person,token,useSessionCookie=false,onRenamed}:{person:Person;token?:string|null;useSessionCookie?:boolean;onRenamed:()=>void}){
  const [name,setName]=useState(person.name||"");
  const [busy,setBusy]=useState(false);
  const [merged,setMerged]=useState<number>(0);
  useEffect(()=>{setName(person.name||"")},[person.name]);

  const patch=async(body:Record<string,unknown>)=>{
    setBusy(true);
    try{
      const response=await fetch(`${API}/api/v1/persons/${person.id}`,{
        method:"PATCH",headers:authHeaders(token),body:JSON.stringify(body),
        credentials:"include",
      });
      // Naming is the moment duplicate clusters of the same person get
      // folded together, so tell the user it happened.
      const result=await response.json().catch(()=>null);
      setMerged(result?.merged_person_ids?.length||0);
      onRenamed();
    }finally{setBusy(false);}
  };
  const save=()=>patch({name});

  return <li className="person-row">
    <div className="person-avatar">
      {person.photo_url
        ? <ZoomablePhoto src={mediaUrl(person.photo_url)!}
            loginUrl={mediaUrl("/api/v1/auth/login")}
            accessToken={token}
            useSessionCookie={useSessionCookie}
            requiresAuth
            alt={person.display_name} title={person.display_name}/>
        : <span className="muted">?</span>}
    </div>
    <div className="person-info">
      <h4>{person.display_name}<TrustBadge trust={person.trust}/></h4>
      <p className="muted">
        Seen {person.sighting_count} {person.sighting_count===1?"time":"times"}
        {person.last_seen_at?` · last ${new Date(person.last_seen_at).toLocaleDateString()}`:""}
      </p>
      {merged>0&&<p className="merged-note">
        Merged {merged} other {merged===1?"sighting group":"sighting groups"} of the same person.
      </p>}
    </div>
    <div className="person-rename">
      <input value={name} disabled={busy} placeholder="Add a name"
        aria-label={`Name for ${person.display_name}`}
        onChange={e=>setName(e.target.value)}
        onKeyDown={e=>{if(e.key==="Enter") save();}}/>
      <button type="button" disabled={busy} onClick={save}>Save</button>
      {/* A household decision, never something the app infers from a face. */}
      <select value={person.trust||"unknown"} disabled={busy}
        aria-label={`Trust for ${person.display_name}`}
        onChange={e=>patch({trust:e.target.value})}>
        <option value="unknown">Not yet known</option>
        <option value="trusted">Trusted</option>
        <option value="watch">Watch</option>
      </select>
    </div>
  </li>;
}

/** People tab: everyone HomeCam has grouped together, named or not. */
export function PeoplePanel({token,useSessionCookie=false}:{
  token?:string|null;useSessionCookie?:boolean
}={}){
  const [persons,setPersons]=useState<Person[]>([]);
  const [recognition,setRecognition]=useState<Recognition|null>(null);
  const [loading,setLoading]=useState(true);
  const [error,setError]=useState("");

  const load=useCallback(async()=>{
    setLoading(true);
    setError("");
    try{
      const response=await fetch(`${API}/api/v1/persons`,{
        headers:token?{Authorization:`Bearer ${token}`}:{},
        credentials:"include",
      });
      if(!response.ok) throw new Error(`HTTP ${response.status}`);
      const body=await response.json();
      setPersons(body.persons||[]);
      setRecognition(body.recognition||null);
    }catch{
      setPersons([]);
      setRecognition(null);
      setError("People could not be loaded from the local API.");
    }
    finally{setLoading(false);}
  },[token]);
  useEffect(()=>{load()},[load]);

  return <section className="panel people-panel">
    <h3>People</h3>
    {/* Never imply automatic recognition is working when only the offline
        hash fallback is active — it cannot match the same person twice. */}
    {recognition&&!recognition.semantic&&<p className="error">
      Automatic recognition is unavailable (using the “{recognition.backend}” fallback).
      People can still be named manually, but returning visitors will not be matched automatically.
    </p>}
    {loading?<p className="muted">Loading people…</p>
      :error?<div className="inline-error" role="alert"><span>{error}</span><button type="button" onClick={load}>Retry</button></div>
      :persons.length?<ul className="person-list">
        {persons.map(person=><PersonRow key={person.id} person={person} token={token} useSessionCookie={useSessionCookie} onRenamed={load}/>)}
      </ul>
      :<p className="muted">Nobody recognized yet. People appear here once a person is detected on a camera.</p>}
  </section>;
}

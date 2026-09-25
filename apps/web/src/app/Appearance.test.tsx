import {describe,it,expect,vi,beforeEach} from "vitest";
import {render,screen,fireEvent,waitFor} from "@testing-library/react";
import {EventCard,PeoplePanel,TrustBadge,appearanceChips,type EventItem,type Person,type Appearance} from "./People";

const BASE_EVENT:EventItem={
  id:"evt-1",camera_id:"dahua-channel-1",type:"person",
  description:"Person detected on Front Yard",start_time:new Date().toISOString(),
  has_photo:true,photo_url:"/api/v1/events/evt-1/photo",
  photo_caption:"An adult in a dark jacket carrying a parcel.",
  photo_rating:null,person_id:null,person_name:null,person_display_name:null,
};

const APPEARANCE:Appearance={
  person_present:true,age_band:"adult",age_confidence:0.8,build:"tall",
  clothing:"dark jacket",carrying:"a parcel",face_visible:true,
  description:"An adult in a dark jacket carrying a parcel.",
};

const PERSON:Person={
  id:"per-1",name:"Sarah",display_name:"Sarah",named:true,notes:null,trust:"unknown",
  sighting_count:3,reference_samples:2,cover_event_id:"evt-1",
  photo_url:"/api/v1/persons/per-1/photo",first_seen_at:null,last_seen_at:null,
};

function mockJson(body:unknown,status=200){
  global.fetch=vi.fn(async()=>new Response(JSON.stringify(body),{status})) as typeof fetch;
}

describe("appearance chips",()=>{
  it("describes only what a witness could have seen",()=>{
    const labels=appearanceChips(APPEARANCE).map(chip=>chip.label.toLowerCase());
    expect(labels.join(" ")).toContain("dark jacket");
    expect(labels.join(" ")).toContain("parcel");
  });

  it("never claims ethnicity or gender",()=>{
    // These are protected attributes the app deliberately refuses to infer:
    // pairing a guess about someone's race with a trust flag is profiling.
    const text=appearanceChips(APPEARANCE).map(chip=>chip.label).join(" ").toLowerCase();
    for(const forbidden of ["male","female","man","woman","white","black","asian"]){
      expect(text).not.toContain(forbidden);
    }
  });

  it("hedges an age it is not sure about",()=>{
    const chips=appearanceChips({...APPEARANCE,age_confidence:0.3});
    expect(chips.find(chip=>chip.key==="age")?.label).toContain("unsure");
  });

  it("says nothing at all when the model returned nothing",()=>{
    expect(appearanceChips({person_present:true})).toHaveLength(0);
  });

  it("reports an obscured face rather than inventing details",()=>{
    const chips=appearanceChips({person_present:true,face_visible:false});
    expect(chips.map(chip=>chip.label)).toContain("Face not visible");
  });
});

describe("appearance in the events section",()=>{
  beforeEach(()=>{mockJson({})});

  it("shows the observable details alongside the photo",()=>{
    render(<EventCard event={{...BASE_EVENT,appearance:APPEARANCE}} persons={[]} onChanged={()=>{}}/>);
    expect(screen.getByText("dark jacket")).toBeInTheDocument();
    expect(screen.getByText("Carrying a parcel")).toBeInTheDocument();
    expect(screen.getByText(/Looks adult/)).toBeInTheDocument();
  });

  it("stays quiet for events recorded before appearance analysis existed",()=>{
    const {container}=render(<EventCard event={BASE_EVENT} persons={[]} onChanged={()=>{}}/>);
    expect(container.querySelector(".appearance")).toBeNull();
  });

  it("admits when nothing was confirmed in the photo",()=>{
    render(<EventCard event={{...BASE_EVENT,photo_verified:false}} persons={[]} onChanged={()=>{}}/>);
    expect(screen.getByText(/no person or animal confirmed/i)).toBeInTheDocument();
  });

  it("does not cast doubt when a subject was confirmed",()=>{
    const {container}=render(
      <EventCard event={{...BASE_EVENT,photo_verified:true}} persons={[]} onChanged={()=>{}}/>
    );
    expect(container.querySelector(".unconfirmed")).toBeNull();
  });

  it("does not cast doubt when nobody checked",()=>{
    const {container}=render(<EventCard event={BASE_EVENT} persons={[]} onChanged={()=>{}}/>);
    expect(container.querySelector(".unconfirmed")).toBeNull();
  });
});

describe("trust",()=>{
  it("stays silent until a human has decided",()=>{
    const {container}=render(<TrustBadge trust="unknown"/>);
    expect(container.firstChild).toBeNull();
  });

  it("marks someone the household trusts",()=>{
    render(<TrustBadge trust="trusted"/>);
    expect(screen.getByText("Trusted")).toBeInTheDocument();
  });

  it("marks someone the household wants flagged",()=>{
    render(<TrustBadge trust="watch"/>);
    expect(screen.getByText("Watch")).toBeInTheDocument();
  });

  it("shows the trust of a recognized person on their event",()=>{
    mockJson({});
    render(
      <EventCard
        event={{...BASE_EVENT,person_id:"per-1",person_display_name:"Sarah",person_trust:"trusted"}}
        persons={[PERSON]} onChanged={()=>{}}/>
    );
    expect(screen.getByText("Trusted")).toBeInTheDocument();
  });
});

describe("trust is a human decision",()=>{
  it("sends the chosen trust level to the API and reports merges",async()=>{
    const calls:Array<{url:string;body:Record<string,unknown>|null}>=[];
    global.fetch=vi.fn(async(url:RequestInfo|URL,init?:RequestInit)=>{
      calls.push({url:String(url),body:init?.body?JSON.parse(String(init.body)):null});
      if(String(url).endsWith("/api/v1/persons")){
        return new Response(JSON.stringify({persons:[PERSON],recognition:null}),{status:200});
      }
      return new Response(JSON.stringify({merged_person_ids:[]}),{status:200});
    }) as typeof fetch;

    render(<PeoplePanel/>);
    const select=await screen.findByLabelText("Trust for Sarah");
    fireEvent.change(select,{target:{value:"trusted"}});
    await waitFor(()=>{
      expect(calls.some(call=>call.body?.trust==="trusted")).toBe(true);
    });
  });

  it("tells the user when naming folded other sighting groups together",async()=>{
    global.fetch=vi.fn(async(url:RequestInfo|URL)=>{
      if(String(url).endsWith("/api/v1/persons")){
        return new Response(
          JSON.stringify({persons:[{...PERSON,name:null,display_name:"Unknown person 00A2",named:false}],recognition:null}),
          {status:200},
        );
      }
      return new Response(JSON.stringify({merged_person_ids:["per-9","per-12"]}),{status:200});
    }) as typeof fetch;

    render(<PeoplePanel/>);
    const input=await screen.findByLabelText("Name for Unknown person 00A2");
    fireEvent.change(input,{target:{value:"Sarah"}});
    fireEvent.keyDown(input,{key:"Enter"});
    expect(await screen.findByText(/Merged 2 other sighting groups/)).toBeInTheDocument();
  });
});

import {describe,it,expect,vi,beforeEach} from "vitest";
import {render,screen,fireEvent,waitFor} from "@testing-library/react";
import {EventCard,PeoplePanel,type EventItem,type Person} from "./People";

const PERSON:Person={
  id:"per-1",name:"Sarah",display_name:"Sarah",named:true,notes:null,
  sighting_count:3,reference_samples:2,cover_event_id:"evt-1",
  photo_url:"/api/v1/persons/per-1/photo",first_seen_at:null,last_seen_at:null,
};
const UNNAMED:Person={
  ...PERSON,id:"per-2",name:null,display_name:"Unknown person 00A2",named:false,
  sighting_count:1,cover_event_id:null,photo_url:null,
};
const EVENT:EventItem={
  id:"evt-1",camera_id:"dahua-channel-1",type:"person",
  description:"Person detected on Front Yard",start_time:new Date().toISOString(),
  has_photo:true,photo_url:"/api/v1/events/evt-1/photo",
  photo_caption:"An adult in a dark jacket carrying a parcel.",
  photo_rating:null,person_id:null,person_name:null,person_display_name:null,
};

function mockJson(body:unknown,status=200){
  global.fetch=vi.fn(async()=>new Response(JSON.stringify(body),{status})) as typeof fetch;
}

describe("event photo card",()=>{
  beforeEach(()=>{mockJson({})});

  it("shows the detected person's photo, not a server-side file path",()=>{
    render(<EventCard event={EVENT} persons={[]} onChanged={()=>{}}/>);
    const image=screen.getByRole("img") as HTMLImageElement;
    expect(image.src).toContain("/api/v1/events/evt-1/photo");
    expect(screen.queryByText(/media[\\/]best-photos/)).not.toBeInTheDocument();
  });

  it("shows the caption so a small crop is understandable",()=>{
    render(<EventCard event={EVENT} persons={[]} onChanged={()=>{}}/>);
    expect(screen.getByText(/An adult in a dark jacket carrying a parcel/)).toBeInTheDocument();
  });

  it("uses the caption as alt text for screen readers",()=>{
    render(<EventCard event={EVENT} persons={[]} onChanged={()=>{}}/>);
    expect(screen.getByAltText("An adult in a dark jacket carrying a parcel.")).toBeInTheDocument();
  });

  it("says so when no photo was captured rather than showing a broken image",()=>{
    render(<EventCard event={{...EVENT,has_photo:false,photo_url:null}} persons={[]} onChanged={()=>{}}/>);
    expect(screen.getByText("No photo captured")).toBeInTheDocument();
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
  });

  it("submits a star rating for the photo",async()=>{
    const onChanged=vi.fn();
    render(<EventCard event={EVENT} persons={[]} onChanged={onChanged}/>);

    fireEvent.click(screen.getByLabelText("Rate 4 out of 5"));

    await waitFor(()=>expect(global.fetch).toHaveBeenCalled());
    const [url,init]=(global.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(String(url)).toContain("/api/v1/events/evt-1/rating");
    expect(JSON.parse(String((init as RequestInit).body))).toEqual({rating:4});
    await waitFor(()=>expect(onChanged).toHaveBeenCalled());
  });

  it("clears the rating when the same star is clicked again",async()=>{
    render(<EventCard event={{...EVENT,photo_rating:3}} persons={[]} onChanged={()=>{}}/>);

    fireEvent.click(screen.getByLabelText("Rate 3 out of 5"));

    await waitFor(()=>expect(global.fetch).toHaveBeenCalled());
    const [,init]=(global.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(JSON.parse(String((init as RequestInit).body))).toEqual({rating:null});
  });

  it("names a newly seen person",async()=>{
    render(<EventCard event={EVENT} persons={[]} onChanged={()=>{}}/>);

    fireEvent.change(screen.getByLabelText("Name this person"),{target:{value:"Alex"}});
    fireEvent.click(screen.getByText("Save name"));

    await waitFor(()=>expect(global.fetch).toHaveBeenCalled());
    const [url,init]=(global.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(String(url)).toContain("/api/v1/events/evt-1/person");
    expect(JSON.parse(String((init as RequestInit).body))).toEqual({name:"Alex"});
  });

  it("assigns the event to an already-known person",async()=>{
    render(<EventCard event={EVENT} persons={[PERSON]} onChanged={()=>{}}/>);

    fireEvent.change(screen.getByLabelText("Assign a known person"),{target:{value:"per-1"}});

    await waitFor(()=>expect(global.fetch).toHaveBeenCalled());
    const [,init]=(global.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(JSON.parse(String((init as RequestInit).body))).toEqual({person_id:"per-1"});
  });

  it("will not submit an empty name",()=>{
    render(<EventCard event={EVENT} persons={[]} onChanged={()=>{}}/>);
    expect(screen.getByText("Save name")).toBeDisabled();
  });

  it("distinguishes an automatic match from a human-confirmed one",()=>{
    const {rerender}=render(
      <EventCard event={{...EVENT,person_id:"per-1",person_display_name:"Sarah",person_confidence:0.91}}
        persons={[PERSON]} onChanged={()=>{}}/>
    );
    expect(screen.getByText("Auto-matched 91%")).toBeInTheDocument();

    rerender(
      <EventCard event={{...EVENT,person_id:"per-1",person_display_name:"Sarah",person_confirmed:true}}
        persons={[PERSON]} onChanged={()=>{}}/>
    );
    expect(screen.getByText("Confirmed")).toBeInTheDocument();
  });
});

describe("people panel",()=>{
  it("lists recognized people with their sighting counts",async()=>{
    mockJson({recognition:{enabled:true,backend:"azure-vision-multimodal",semantic:true,match_threshold:0.86},persons:[PERSON]});
    render(<PeoplePanel/>);

    expect(await screen.findByText("Sarah")).toBeInTheDocument();
    expect(screen.getByText(/Seen 3 times/)).toBeInTheDocument();
  });

  it("gives unnamed people a usable label and a way to name them",async()=>{
    mockJson({recognition:{enabled:true,backend:"azure-vision-multimodal",semantic:true,match_threshold:0.86},persons:[UNNAMED]});
    render(<PeoplePanel/>);

    expect(await screen.findByText("Unknown person 00A2")).toBeInTheDocument();
    expect(screen.getByLabelText("Name for Unknown person 00A2")).toBeInTheDocument();
  });

  it("renames a person",async()=>{
    mockJson({recognition:{enabled:true,backend:"azure-vision-multimodal",semantic:true,match_threshold:0.86},persons:[UNNAMED]});
    render(<PeoplePanel/>);
    await screen.findByText("Unknown person 00A2");

    fireEvent.change(screen.getByLabelText("Name for Unknown person 00A2"),{target:{value:"Postman"}});
    fireEvent.click(screen.getByText("Save"));

    await waitFor(()=>{
      const calls=(global.fetch as ReturnType<typeof vi.fn>).mock.calls;
      const patch=calls.find(call=>(call[1] as RequestInit|undefined)?.method==="PATCH");
      expect(patch).toBeDefined();
      expect(String(patch?.[0])).toContain("/api/v1/persons/per-2");
      expect(JSON.parse(String((patch?.[1] as RequestInit).body))).toEqual({name:"Postman"});
    });
  });

  it("warns when automatic recognition is not actually available",async()=>{
    mockJson({recognition:{enabled:true,backend:"local-hash",semantic:false,match_threshold:0.86},persons:[]});
    render(<PeoplePanel/>);

    expect(await screen.findByText(/Automatic recognition is unavailable/i)).toBeInTheDocument();
  });

  it("does not warn when a real recognition backend is configured",async()=>{
    mockJson({recognition:{enabled:true,backend:"azure-vision-multimodal",semantic:true,match_threshold:0.86},persons:[PERSON]});
    render(<PeoplePanel/>);

    await screen.findByText("Sarah");
    expect(screen.queryByText(/Automatic recognition is unavailable/i)).not.toBeInTheDocument();
  });

  it("shows an empty state before anyone has been seen",async()=>{
    mockJson({recognition:{enabled:true,backend:"azure-vision-multimodal",semantic:true,match_threshold:0.86},persons:[]});
    render(<PeoplePanel/>);

    expect(await screen.findByText(/Nobody recognized yet/i)).toBeInTheDocument();
  });
});

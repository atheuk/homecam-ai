import {describe,it,expect,vi,beforeEach,afterEach} from "vitest";
import {render,screen,fireEvent,waitFor} from "@testing-library/react";
import EventClip,{clipFacts,type EventClipInfo} from "./EventClip";
import EventsPanel from "./EventsPanel";
import {EventCard,animalDetails,describeAnimal,type EventItem} from "./People";

const READY:EventClipInfo={
  status:"ready",url:"/api/v1/events/evt-1/clip",source:"stream",
  duration_seconds:16.2,pre_roll_seconds:8,codec:"avc1.640028",width:1920,height:1080,size_bytes:2_000_000,
};
const EDGE:EventClipInfo={...READY,source:"edge",pre_roll_seconds:0,duration_seconds:14.8};

const EVENT:EventItem={
  id:"evt-1",camera_id:"eufy-T8210",type:"doorbell",description:"Doorbell pressed",
  start_time:new Date().toISOString(),person_id:null,person_name:null,person_display_name:null,
};

describe("event clips",()=>{
  beforeEach(()=>{
    global.URL.createObjectURL=vi.fn(()=>"blob:clip");
    global.URL.revokeObjectURL=vi.fn();
  });
  afterEach(()=>vi.restoreAllMocks());

  it("says honestly how long the clip is and how much came before the event",()=>{
    expect(clipFacts(READY)).toBe("16.2 s clip · starts 8 s before the event · 1080p");
    expect(clipFacts(EDGE)).toBe("14.8 s clip · starts when the camera woke (no earlier footage) · 1080p");
  });

  it("fetches the clip with authentication and plays it from a blob",async()=>{
    const fetchMock=vi.fn(async()=>new Response(new Blob(["mp4"]),{status:200}));
    global.fetch=fetchMock as unknown as typeof fetch;
    render(<EventClip eventId="evt-1" clip={READY} token="tok"/>);
    fireEvent.click(screen.getByRole("button",{name:/Play clip/}));
    await waitFor(()=>expect(screen.getByLabelText("Event clip")).toHaveAttribute("src","blob:clip"));
    const [url,init]=fetchMock.mock.calls[0] as unknown as [string,RequestInit];
    expect(url).toMatch(/\/api\/v1\/events\/evt-1\/clip$/);
    expect((init.headers as Record<string,string>).Authorization).toBe("Bearer tok");
    expect(init.credentials).toBe("include");
  });

  it("refuses a clip URL on another origin",async()=>{
    const fetchMock=vi.fn();
    global.fetch=fetchMock as unknown as typeof fetch;
    render(<EventClip eventId="evt-1" clip={{...READY,url:"https://evil.example/clip.mp4"}} token="tok"/>);
    fireEvent.click(screen.getByRole("button",{name:/Play clip/}));
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not load this clip");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it.each([
    ["pending","Recording clip…"],
    ["unsupported","This camera cannot record event clips"],
    ["skipped","Clip skipped: daily or storage limit reached"],
    ["expired","Clip expired"],
    ["unavailable","No clip for this event"],
  ] as const)("distinguishes the %s state",(status,text)=>{
    render(<EventClip eventId="evt-1" clip={{status,url:null,reason:status==="unavailable"?"No buffered live video":null}}/>);
    expect(screen.getByRole("status")).toHaveTextContent(text);
    expect(screen.queryByRole("button",{name:/Play clip/})).not.toBeInTheDocument();
  });

  it("shows nothing for events that never had a clip",()=>{
    const {container}=render(<EventClip eventId="evt-1" clip={{status:"none",url:null}}/>);
    expect(container).toBeEmptyDOMElement();
  });

  it("is shown on the normal event card, not only in Security",()=>{
    render(<EventCard event={{...EVENT,clip:EDGE}} persons={[]} onChanged={()=>{}}/>);
    expect(screen.getByRole("button",{name:/Play clip/})).toBeInTheDocument();
  });
});

describe("events filters for clips and animals",()=>{
  const events:EventItem[]=[
    {...EVENT,id:"with-clip",description:"Ring with clip",clip:READY},
    {...EVENT,id:"no-clip",description:"Ring without clip",clip:{status:"unavailable",url:null}},
    {...EVENT,id:"dog",type:"animal",description:"Dog on lawn",animal:{species:"dog",breed:"Labrador",breed_certainty:"possible",confidence:0.5}},
    {...EVENT,id:"cat",type:"animal",description:"Cat on wall",animal:{species:"cat",confidence:0.9}},
  ];
  const props={events,persons:[],cameras:[],token:null,newEventIds:new Set<string>(),onChanged:()=>{},onAcknowledgeNew:()=>{}};

  it("shows only events with a playable clip",()=>{
    render(<EventsPanel {...props}/>);
    fireEvent.click(screen.getByLabelText("With video clip"));
    expect(screen.getByText("Ring with clip")).toBeInTheDocument();
    expect(screen.queryByText("Ring without clip")).not.toBeInTheDocument();
    expect(screen.queryByText("Dog on lawn")).not.toBeInTheDocument();
  });

  it("filters doorbell rings and animals by species",()=>{
    render(<EventsPanel {...props}/>);
    fireEvent.click(screen.getByRole("button",{name:"doorbell"}));
    expect(screen.queryByText("Dog on lawn")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button",{name:"animal"}));
    fireEvent.change(screen.getByLabelText("Animal"),{target:{value:"cat"}});
    expect(screen.getByText("Cat on wall")).toBeInTheDocument();
    expect(screen.queryByText("Dog on lawn")).not.toBeInTheDocument();
  });
});

describe("richer animal descriptions",()=>{
  it("hedges breed and counts animals",()=>{
    expect(describeAnimal({species:"dog",breed:"Border Collie",breed_certainty:"likely",count:2})).toBe("2 × Dog · likely Border Collie");
    expect(describeAnimal({species:"cat",breed:"Maine Coon",breed_certainty:"possible"})).toBe("Cat · possibly Maine Coon");
    expect(describeAnimal({species:"other"})).toBe("Unrecognized animal");
  });

  it("lists only visible coat, size and behaviour",()=>{
    expect(animalDetails({species:"cat",coat_colours:["orange","white"],coat_pattern:"tabby",size:"small",action:"sitting on the wall",collar_visible:true}).map(d=>d.label))
      .toEqual(["orange & white tabby coat","small size","sitting on the wall","Collar visible"]);
    expect(animalDetails({species:"bird"})).toEqual([]);
  });

  it("says a breed is a suggestion, not an identity",()=>{
    render(<EventCard event={{...EVENT,type:"animal",animal:{species:"dog",breed:"Labrador",breed_certainty:"possible",confidence:0.5}}} persons={[]} onChanged={()=>{}}/>);
    expect(screen.getByText("Breed is a suggestion, not a confirmed identity")).toBeInTheDocument();
  });
});

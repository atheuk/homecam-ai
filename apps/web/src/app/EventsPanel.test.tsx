import {describe,expect,it,vi} from "vitest";
import {fireEvent,render,screen} from "@testing-library/react";
import EventsPanel from "./EventsPanel";
import type {EventItem,Person} from "./People";

const cameras=[
  {id:"front",name:"Front Door"},
  {id:"garden",name:"Garden"},
];
const people:Person[]=[{
  id:"person-1",name:"Sarah",display_name:"Sarah",named:true,notes:null,
  sighting_count:3,reference_samples:2,cover_event_id:null,photo_url:null,
  first_seen_at:null,last_seen_at:null,
}];
const events:EventItem[]=[
  {id:"person",camera_id:"front",type:"person",description:"Sarah arrived",
    start_time:"2026-09-28T12:00:00Z",person_id:"person-1",person_display_name:"Sarah"},
  {id:"vehicle",camera_id:"garden",type:"vehicle",description:"Vehicle in garden",
    start_time:"2026-09-28T11:00:00Z"},
  {id:"motion",camera_id:"front",type:"motion",description:"Motion at the door",
    start_time:"2026-09-27T20:00:00Z"},
];

function renderPanel(newEventIds=new Set<string>()){
  return render(<EventsPanel events={events} persons={people} cameras={cameras}
    newEventIds={newEventIds} onChanged={()=>{}} onAcknowledgeNew={vi.fn()}/>);
}

describe("events panel",()=>{
  it("filters by event type",()=>{
    renderPanel();
    fireEvent.click(screen.getByRole("button",{name:"person"}));
    expect(screen.getByText("Sarah arrived")).toBeInTheDocument();
    expect(screen.queryByText("Vehicle in garden")).not.toBeInTheDocument();
  });

  it("filters by camera",()=>{
    renderPanel();
    fireEvent.change(screen.getByLabelText("Camera"),{target:{value:"garden"}});
    expect(screen.getByText("Vehicle in garden")).toBeInTheDocument();
    expect(screen.queryByText("Sarah arrived")).not.toBeInTheDocument();
  });

  it("can show only events assigned to named people",()=>{
    renderPanel();
    fireEvent.click(screen.getByLabelText("Named people only"));
    expect(screen.getByText("Sarah arrived")).toBeInTheDocument();
    expect(screen.queryByText("Vehicle in garden")).not.toBeInTheDocument();
  });

  it("groups events by day",()=>{
    renderPanel();
    expect(screen.getByRole("heading",{name:"Today"})).toBeInTheDocument();
    expect(screen.getByRole("heading",{name:"Yesterday"})).toBeInTheDocument();
  });

  it("surfaces newly received events",()=>{
    renderPanel(new Set(["person"]));
    expect(screen.getByRole("button",{name:/1 new event received/i})).toBeInTheDocument();
    expect(screen.getByText("Sarah arrived").closest(".event-arrival")).not.toBeNull();
  });
});

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
  {id:"parked",camera_id:"garden",type:"vehicle",description:"A car arrived in the Driveway",
    start_time:"2026-09-27T19:00:00Z",tags:["car","vehicle_arrived","vehicle_parked"],
    scene:{kind:"vehicle",transition:"arrived",zone:"Driveway",parked:true}},
  {id:"mail",camera_id:"front",type:"package",description:"Mail was put in the Mailbox",
    start_time:"2026-09-27T18:00:00Z",tags:["mailbox","mailbox_delivery","mail"],
    scene:{kind:"mailbox",transition:"mailbox_delivery",zone:"Mailbox",item_type:"mail"}},
  {id:"mail-out",camera_id:"front",type:"package",description:"Mail was taken out of the Mailbox",
    start_time:"2026-09-27T17:00:00Z",tags:["mailbox","mailbox_retrieval","mail"],
    scene:{kind:"mailbox",transition:"mailbox_retrieval",zone:"Mailbox",item_type:"mail"}},
  {id:"mail-open",camera_id:"front",type:"package",description:"The Mailbox was opened",
    start_time:"2026-09-27T16:00:00Z",tags:["mailbox","mailbox_opened"]},
];

function renderPanel(newEventIds=new Set<string>()){
  return render(<EventsPanel events={events} persons={people} cameras={cameras}
    newEventIds={newEventIds} onChanged={()=>{}} onAcknowledgeNew={vi.fn()}/>);
}

describe("events panel",()=>{
  it("shows what changed as badges, not raw tracker state",()=>{
    renderPanel();
    expect(screen.getByText("Arrived")).toBeInTheDocument();
    expect(screen.getByText("Parked")).toBeInTheDocument();
    expect(screen.getByText("Mail delivered")).toBeInTheDocument();
    expect(screen.getByText("Mail taken out")).toBeInTheDocument();
    expect(screen.queryByText(/vehicle_parked/)).not.toBeInTheDocument();
  });

  it("filters by activity",()=>{
    renderPanel();
    fireEvent.change(screen.getByLabelText("Activity"),{target:{value:"mailbox"}});
    expect(screen.getByText("Mail was put in the Mailbox")).toBeInTheDocument();
    expect(screen.getByText("Mail was taken out of the Mailbox")).toBeInTheDocument();
    expect(screen.getByText("The Mailbox was opened")).toBeInTheDocument();
    expect(screen.queryByText("A car arrived in the Driveway")).not.toBeInTheDocument();
    expect(screen.queryByText("Sarah arrived")).not.toBeInTheDocument();
  });

  it("filters packages by type",()=>{
    renderPanel();
    fireEvent.click(screen.getByRole("button",{name:"package"}));
    expect(screen.getByText("Mail was put in the Mailbox")).toBeInTheDocument();
    expect(screen.queryByText("Motion at the door")).not.toBeInTheDocument();
  });
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
    // The fixtures carry fixed dates, so pin "now" to the day they describe.
    vi.useFakeTimers({toFake:["Date"]});
    vi.setSystemTime(new Date("2026-09-28T13:00:00Z"));
    try{
      renderPanel();
      expect(screen.getByRole("heading",{name:"Today"})).toBeInTheDocument();
      expect(screen.getByRole("heading",{name:"Yesterday"})).toBeInTheDocument();
    }finally{
      vi.useRealTimers();
    }
  });

  it("surfaces newly received events",()=>{
    renderPanel(new Set(["person"]));
    expect(screen.getByRole("button",{name:/1 new event received/i})).toBeInTheDocument();
    expect(screen.getByText("Sarah arrived").closest(".event-arrival")).not.toBeNull();
  });
});

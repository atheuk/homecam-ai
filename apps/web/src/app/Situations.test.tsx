import {describe,expect,it,vi} from "vitest";
import {fireEvent,render,screen} from "@testing-library/react";
import EventsPanel from "./EventsPanel";
import {EventCard,situationBadges,type EventItem} from "./People";

const events:EventItem[]=[
  {id:"mail",camera_id:"front",type:"package",description:"A letter was delivered to the mailbox",
    start_time:"2026-09-28T12:00:00Z",tags:["mailbox","mailbox_delivery","letter"],zone:"Mailbox",
    metadata:{temporal:{kind:"mailbox",evidence:{
      before:"/api/v1/events/mail/evidence/before",after:"/api/v1/events/mail/evidence/after",
    }}}},
  {id:"bins",camera_id:"front",type:"motion",description:"Bins were emptied at the kerb",
    start_time:"2026-09-28T11:00:00Z",tags:["bins","bin_emptied"]},
  {id:"car",camera_id:"front",type:"vehicle",description:"A vehicle arrived and parked",
    start_time:"2026-09-28T10:00:00Z",tags:["car","vehicle_arrived","vehicle_parked"]},
  {id:"walk",camera_id:"front",type:"person",description:"Person detected",
    start_time:"2026-09-28T09:00:00Z",tags:["person"]},
];

function renderPanel(){
  return render(<EventsPanel events={events} persons={[]} cameras={[{id:"front",name:"Front"}]}
    newEventIds={new Set()} onChanged={()=>{}} onAcknowledgeNew={vi.fn()}/>);
}

describe("situations",()=>{
  it("labels situation tags in plain language and ignores the rest",()=>{
    expect(situationBadges(events[0]).map(item=>item.label)).toEqual(["Mail delivered","Letter"]);
    expect(situationBadges(events[2]).map(item=>item.label)).toEqual(["Arrived","Parked"]);
    expect(situationBadges(events[3])).toEqual([]);
    expect(situationBadges({tags:null})).toEqual([]);
  });

  it.each([
    ["Mail","A letter was delivered to the mailbox"],
    ["Bins","Bins were emptied at the kerb"],
    ["Parked cars","A vehicle arrived and parked"],
  ])("filters the %s situation by tag",(label,shown)=>{
    renderPanel();
    fireEvent.click(screen.getByRole("button",{name:label}));
    expect(screen.getByText(shown)).toBeInTheDocument();
    expect(screen.queryByText("Person detected")).not.toBeInTheDocument();
    expect(screen.getByText("1 shown")).toBeInTheDocument();
  });

  it("shows badges, evidence links and what was not confirmed",()=>{
    const unsure:EventItem={...events[0],type:"motion",tags:["mailbox","mailbox_activity"],
      metadata:{temporal:{kind:"mailbox",unknowns:["vision check was not confident enough"],evidence:{
        during:"/api/v1/events/mail/evidence/during",
      }}}};
    render(<EventCard event={unsure} persons={[]} onChanged={()=>{}}/>);
    expect(screen.getByText("At mailbox")).toBeInTheDocument();
    expect(screen.queryByText("Mail delivered")).not.toBeInTheDocument();
    expect(screen.getByText(/Not confirmed: vision check was not confident enough/)).toBeInTheDocument();
    const link=screen.getByRole("link",{name:"during"});
    expect(link.getAttribute("href")).toMatch(/\/api\/v1\/events\/mail\/evidence\/during$/);
  });
});

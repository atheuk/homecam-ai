import {afterEach,describe,expect,it,vi} from "vitest";
import {fireEvent,render,screen,waitFor} from "@testing-library/react";

import {DigestCard,SearchCard} from "./Insights";

function response(body:unknown,status=200){
  return new Response(JSON.stringify(body),{status,headers:{"Content-Type":"application/json"}});
}

const digestBody={
  date:"2025-05-01",
  summary:"12 events were recorded on 2025-05-01.",
  source:"template",
  stats:{
    event_count:12,
    incident_count:1,
    unusual_count:2,
    loitering_count:3,
    notable:[{event_id:"evt-9",camera_id:"front-door",type:"person",at:new Date().toISOString(),description:"Someone lingered by the door"}],
  },
};

afterEach(()=>vi.restoreAllMocks());

describe("SearchCard",()=>{
  it("shows results for a natural-language query",async()=>{
    global.fetch=vi.fn(async()=>response({
      query:"van in the driveway",refused:false,notice:null,blocked_categories:[],
      results:[{id:"evt-1",camera_id:"driveway",type:"vehicle",description:"A van stopped",start_time:new Date().toISOString()}],
    })) as typeof fetch;
    render(<SearchCard token="tok-1"/>);
    fireEvent.change(screen.getByLabelText("Search events"),{target:{value:"van in the driveway"}});
    fireEvent.click(screen.getByRole("button",{name:"Search"}));
    expect(await screen.findByText("A van stopped")).toBeInTheDocument();
  });

  it("surfaces a moderation refusal instead of hiding it",async()=>{
    global.fetch=vi.fn(async()=>response({
      query:"who is at the door",refused:true,
      notice:"HomeCam cannot identify people. Try describing what happened instead.",
      blocked_categories:["identity"],results:[],
    })) as typeof fetch;
    render(<SearchCard token="tok-1"/>);
    fireEvent.change(screen.getByLabelText("Search events"),{target:{value:"who is at the door"}});
    fireEvent.click(screen.getByRole("button",{name:"Search"}));
    const refusal=await screen.findByTestId("search-refusal");
    expect(refusal).toHaveTextContent("cannot identify people");
    expect(screen.queryByLabelText("Search results")).not.toBeInTheDocument();
  });

  it("reports an empty result set",async()=>{
    global.fetch=vi.fn(async()=>response({query:"x",refused:false,notice:null,results:[]})) as typeof fetch;
    render(<SearchCard token="tok-1"/>);
    fireEvent.change(screen.getByLabelText("Search events"),{target:{value:"x"}});
    fireEvent.click(screen.getByRole("button",{name:"Search"}));
    expect(await screen.findByText("No matching events")).toBeInTheDocument();
  });

  it("explains when search is turned off",async()=>{
    global.fetch=vi.fn(async()=>response({detail:"Search is disabled"},503)) as typeof fetch;
    render(<SearchCard token="tok-1"/>);
    fireEvent.change(screen.getByLabelText("Search events"),{target:{value:"x"}});
    fireEvent.click(screen.getByRole("button",{name:"Search"}));
    expect(await screen.findByText("Search is turned off on this system.")).toBeInTheDocument();
  });

  it("sends the signed-in token with the search request",async()=>{
    const fetchMock=vi.fn(async(_url:RequestInfo|URL,_init?:RequestInit)=>response({query:"x",refused:false,notice:null,results:[]}));
    global.fetch=fetchMock as unknown as typeof fetch;
    render(<SearchCard token="tok-1"/>);
    fireEvent.change(screen.getByLabelText("Search events"),{target:{value:"x"}});
    fireEvent.click(screen.getByRole("button",{name:"Search"}));
    await waitFor(()=>expect(fetchMock).toHaveBeenCalled());
    const headers=fetchMock.mock.calls[0][1]?.headers as Record<string,string>;
    expect(headers.Authorization).toBe("Bearer tok-1");
  });

  it("does not call the API for a blank query",()=>{
    const fetchMock=vi.fn(async(_url:RequestInfo|URL)=>response({}));
    global.fetch=fetchMock as unknown as typeof fetch;
    render(<SearchCard token="tok-1"/>);
    fireEvent.click(screen.getByRole("button",{name:"Search"}));
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe("DigestCard",()=>{
  it("renders the day summary and counts",async()=>{
    global.fetch=vi.fn(async()=>response(digestBody)) as typeof fetch;
    render(<DigestCard token="tok-1"/>);
    expect(await screen.findByTestId("digest-summary")).toHaveTextContent("12 events were recorded");
    expect(screen.getByText("Someone lingered by the door")).toBeInTheDocument();
  });

  it("sends the signed-in token with the digest request",async()=>{
    const fetchMock=vi.fn(async(_url:RequestInfo|URL,_init?:RequestInit)=>response(digestBody));
    global.fetch=fetchMock as unknown as typeof fetch;
    render(<DigestCard token="tok-1"/>);
    await screen.findByTestId("digest-summary");
    const headers=fetchMock.mock.calls[0][1]?.headers as Record<string,string>;
    expect(headers.Authorization).toBe("Bearer tok-1");
  });

  it("refetches with refresh=true when asked",async()=>{
    const fetchMock=vi.fn(async(_url:RequestInfo|URL)=>response(digestBody));
    global.fetch=fetchMock as unknown as typeof fetch;
    render(<DigestCard token="tok-1"/>);
    await screen.findByTestId("digest-summary");
    fireEvent.click(screen.getByRole("button",{name:"Refresh"}));
    await waitFor(()=>expect(String(fetchMock.mock.calls[1][0])).toContain("refresh=true"));
  });

  it("renders nothing when the digest feature is disabled",async()=>{
    global.fetch=vi.fn(async()=>response({detail:"Digest is disabled"},503)) as typeof fetch;
    const {container}=render(<DigestCard token="tok-1"/>);
    await waitFor(()=>expect(container.querySelector(".digest-panel")).toBeNull());
  });

  it("degrades to a message when the API fails",async()=>{
    global.fetch=vi.fn(async()=>{throw new Error("offline");}) as typeof fetch;
    render(<DigestCard token="tok-1"/>);
    expect(await screen.findByText("The digest is unavailable right now.")).toBeInTheDocument();
  });
});

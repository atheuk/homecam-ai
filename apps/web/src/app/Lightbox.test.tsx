import {describe,it,expect,vi,afterEach} from "vitest";
import {render,screen,fireEvent,waitFor,act,cleanup} from "@testing-library/react";
import {ZoomablePhoto,Lightbox} from "./Lightbox";

const PHOTO={
  src:"http://api.test/api/v1/events/evt-1/photo",
  alt:"An adult in a dark jacket carrying a parcel.",
  caption:"An adult in a dark jacket carrying a parcel.",
  title:"Sarah",
};

function viewerImage():HTMLImageElement{
  const image=document.querySelector(".lightbox-viewport img");
  if(!image) throw new Error("the full-screen viewer is not showing a photo");
  return image as HTMLImageElement;
}

/** The transformed wrapper: zoom/pan is applied here so the photo and its
 * detection borders move together. */
function viewerFigure():HTMLElement{
  const figure=document.querySelector(".lightbox-figure");
  if(!figure) throw new Error("the full-screen viewer is not showing a photo");
  return figure as HTMLElement;
}

function viewport():Element{
  const element=document.querySelector(".lightbox-viewport");
  if(!element) throw new Error("the full-screen viewer is not open");
  return element;
}

/** Pointer events rely on the PointerEvent shim installed in test-setup,
 * without which jsdom drops clientX/clientY and every drag assertion
 * silently passes against a stationary photo. */
function pointer(type:"pointerdown"|"pointermove"|"pointerup",target:Element,init:{pointerId:number;clientX?:number;clientY?:number}){
  fireEvent[type==="pointerdown"?"pointerDown":type==="pointermove"?"pointerMove":"pointerUp"](target,{
    pointerId:init.pointerId,clientX:init.clientX??0,clientY:init.clientY??0,
  });
}
function openViewer(){
  render(<ZoomablePhoto {...PHOTO}/>);
  fireEvent.click(screen.getByRole("button",{name:/open sarah full screen/i}));
  return screen.getByRole("dialog");
}

describe("opening a photo full screen",()=>{
  it("does not request the protected image when sign-in fails",async()=>{
    const fetchMock=vi.fn(async()=>new Response(null,{status:401}));
    vi.stubGlobal("fetch",fetchMock);
    render(<ZoomablePhoto {...PHOTO} requiresAuth fullSrc="http://api.test/api/v1/events/evt-1/photo/full"
      loginUrl="http://api.test/api/v1/auth/login"/>);
    fireEvent.click(screen.getByRole("button",{name:/open sarah full screen/i}));
    fireEvent.change(screen.getByRole("textbox",{name:"Photo account email"}),{target:{value:"owner@example.com"}});
    fireEvent.change(screen.getByLabelText("Photo account password"),{target:{value:"incorrect"}});
    fireEvent.click(screen.getByRole("button",{name:"View photo"}));
    await waitFor(()=>expect(screen.getByRole("alert")).toHaveTextContent("Sign-in failed"));
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(document.querySelector(".lightbox-viewport img")).not.toBeInTheDocument();
    vi.unstubAllGlobals();
  });

  it("fetches protected crops and full frames with the bearer token and revokes both URLs",async()=>{
    const cropUrl="blob:protected-crop";
    const fullUrl="blob:protected-full";
    const createObjectURL=vi.fn()
      .mockReturnValueOnce(cropUrl)
      .mockReturnValueOnce(fullUrl);
    const revokeObjectURL=vi.fn();
    vi.stubGlobal("URL",{createObjectURL,revokeObjectURL});
    const fetchMock=vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({access_token:"token-1"}),{status:200}))
      .mockResolvedValueOnce(new Response(new Blob(["crop"]),{status:200}))
      .mockResolvedValueOnce(new Response(new Blob(["full"]),{status:200}));
    vi.stubGlobal("fetch",fetchMock);
    const fullSrc="http://api.test/api/v1/events/evt-1/photo/full";
    render(<ZoomablePhoto {...PHOTO} requiresAuth fullSrc={fullSrc} loginUrl="http://api.test/api/v1/auth/login"/>);
    fireEvent.click(screen.getByRole("button",{name:/open sarah full screen/i}));
    fireEvent.change(screen.getByRole("textbox",{name:"Photo account email"}),{target:{value:"owner@example.com"}});
    fireEvent.change(screen.getByLabelText("Photo account password"),{target:{value:"correct"}});
    fireEvent.click(screen.getByRole("button",{name:"View photo"}));
    await waitFor(()=>expect(viewerImage().src).toBe(cropUrl));
    fireEvent.click(screen.getByRole("button",{name:"View full resolution"}));
    await waitFor(()=>expect(viewerImage().src).toBe(fullUrl));
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(fetchMock.mock.calls[1][0]).toBe(PHOTO.src);
    expect(fetchMock.mock.calls[2][0]).toBe(fullSrc);
    for(const [,init] of fetchMock.mock.calls.slice(1)){
      expect(init).toMatchObject({cache:"no-store"});
      expect((init as RequestInit).headers).toEqual({Authorization:"Bearer token-1"});
    }
    fireEvent.click(screen.getByRole("button",{name:/close full screen/i}));
    await waitFor(()=>{
      expect(revokeObjectURL).toHaveBeenCalledWith(cropUrl);
      expect(revokeObjectURL).toHaveBeenCalledWith(fullUrl);
    });
    vi.unstubAllGlobals();
  });

  it("loads protected photos with the restored session cookie without asking for credentials",async()=>{
    const createObjectURL=vi.fn().mockReturnValue("blob:cookie-photo");
    const revokeObjectURL=vi.fn();
    vi.stubGlobal("URL",{createObjectURL,revokeObjectURL});
    const fetchMock=vi.fn(async()=>new Response(new Blob(["photo"]),{status:200}));
    vi.stubGlobal("fetch",fetchMock);
    const {unmount}=render(<ZoomablePhoto {...PHOTO} requiresAuth useSessionCookie
      fullSrc="http://api.test/api/v1/events/evt-1/photo/full"/>);
    // The crop is fetched for the card itself, so the viewer opens on the
    // photo rather than on another "View photo" step.
    await waitFor(()=>expect(screen.getByAltText(PHOTO.alt)).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button",{name:/open sarah full screen/i}));

    expect(screen.queryByLabelText("Photo account email")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Photo account password")).not.toBeInTheDocument();
    expect(screen.queryByRole("button",{name:"View photo"})).not.toBeInTheDocument();

    await waitFor(()=>expect(viewerImage().src).toBe("blob:cookie-photo"));
    expect(fetchMock).toHaveBeenCalledWith(PHOTO.src,expect.objectContaining({
      credentials:"include",
      headers:{},
      cache:"no-store",
    }));
    unmount();
    await waitFor(()=>expect(revokeObjectURL).toHaveBeenCalledWith("blob:cookie-photo"));
    vi.unstubAllGlobals();
  });

  it("opens the photo in a full-screen dialog when the thumbnail is clicked",()=>{
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    const dialog=openViewer();
    expect(dialog).toHaveAttribute("aria-modal","true");
    // The full-size photo, not just the thumbnail, is now on screen.
    const images=screen.getAllByAltText(PHOTO.alt);
    expect(images.length).toBe(2);
  });

  it("shows the caption so a zoomed crop stays understandable",()=>{
    openViewer();
    expect(screen.getByText(PHOTO.caption)).toBeInTheDocument();
  });

  it("closes on Escape",()=>{
    openViewer();
    fireEvent.keyDown(window,{key:"Escape"});
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("closes with the close button",()=>{
    openViewer();
    fireEvent.click(screen.getByRole("button",{name:/close full screen/i}));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("closes when the backdrop itself is clicked, but not the photo",()=>{
    const dialog=openViewer();
    fireEvent.click(viewerImage());
    expect(screen.queryByRole("dialog")).toBeInTheDocument();

    fireEvent.click(dialog);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("holds the page still while open and releases it afterwards",()=>{
    openViewer();
    expect(document.body.style.overflow).toBe("hidden");
    fireEvent.keyDown(window,{key:"Escape"});
    expect(document.body.style.overflow).not.toBe("hidden");
  });
});

describe("inline preview of a protected photo",()=>{
  const AUTH={...PHOTO,requiresAuth:true as const,fullSrc:"http://api.test/api/v1/events/evt-1/photo/full"};

  // Patch the object-URL helpers on the real URL rather than replacing it:
  // React flushes unmount effects after the test body, and a wholesale stub
  // would be gone by the time the cleanup revokes its blob.
  const originals={createObjectURL:URL.createObjectURL,revokeObjectURL:URL.revokeObjectURL};
  function stubObjectUrls(url="blob:preview"){
    const createObjectURL=vi.fn().mockReturnValue(url);
    const revokeObjectURL=vi.fn();
    URL.createObjectURL=createObjectURL as unknown as typeof URL.createObjectURL;
    URL.revokeObjectURL=revokeObjectURL as unknown as typeof URL.revokeObjectURL;
    return {createObjectURL,revokeObjectURL};
  }

  afterEach(()=>{
    // Unmount before restoring: React's cleanup is what revokes the blob,
    // and jsdom has no real revokeObjectURL to fall back to.
    cleanup();
    vi.unstubAllGlobals();
    URL.createObjectURL=originals.createObjectURL;
    URL.revokeObjectURL=originals.revokeObjectURL;
  });

  it("shows the crop inline using the restored session cookie",async()=>{
    stubObjectUrls("blob:cookie-preview");
    const fetchMock=vi.fn(async()=>new Response(new Blob(["crop"]),{status:200}));
    vi.stubGlobal("fetch",fetchMock);
    render(<ZoomablePhoto {...AUTH} useSessionCookie/>);

    await waitFor(()=>expect(screen.getByAltText(PHOTO.alt)).toHaveAttribute("src","blob:cookie-preview"));
    expect(screen.queryByText("Sign in to view photo")).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url,init]=fetchMock.mock.calls[0] as unknown as [string,RequestInit];
    expect(url).toBe(PHOTO.src);
    expect(init).toMatchObject({credentials:"include",cache:"no-store"});
    expect(init.headers).toEqual({});
  });

  it("sends the bearer token when one is available",async()=>{
    stubObjectUrls("blob:token-preview");
    const fetchMock=vi.fn(async()=>new Response(new Blob(["crop"]),{status:200}));
    vi.stubGlobal("fetch",fetchMock);
    render(<ZoomablePhoto {...AUTH} accessToken="token-1"/>);

    await waitFor(()=>expect(screen.getByAltText(PHOTO.alt)).toHaveAttribute("src","blob:token-preview"));
    const [,init]=fetchMock.mock.calls[0] as unknown as [string,RequestInit];
    expect(init.headers).toEqual({Authorization:"Bearer token-1"});
    expect(init).toMatchObject({credentials:"include",cache:"no-store"});
  });

  it("never points an image at the protected URL for anonymous viewers",()=>{
    const fetchMock=vi.fn();
    vi.stubGlobal("fetch",fetchMock);
    render(<ZoomablePhoto {...AUTH}/>);

    expect(fetchMock).not.toHaveBeenCalled();
    expect(screen.getByText("Sign in to view photo")).toBeInTheDocument();
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
  });

  it("keeps the photo hidden when the session has expired",async()=>{
    const {createObjectURL}=stubObjectUrls();
    const fetchMock=vi.fn(async()=>new Response(null,{status:401}));
    vi.stubGlobal("fetch",fetchMock);
    render(<ZoomablePhoto {...AUTH} useSessionCookie/>);

    await waitFor(()=>expect(screen.getByText("Open photo")).toBeInTheDocument());
    expect(createObjectURL).not.toHaveBeenCalled();
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
  });

  it("waits until the card is near the viewport before fetching",async()=>{
    stubObjectUrls("blob:lazy-preview");
    const fetchMock=vi.fn(async()=>new Response(new Blob(["crop"]),{status:200}));
    vi.stubGlobal("fetch",fetchMock);
    let notify:((entries:{isIntersecting:boolean}[])=>void)|null=null;
    const disconnect=vi.fn();
    class ObserverStub{
      constructor(callback:(entries:{isIntersecting:boolean}[])=>void){notify=callback}
      observe(){}
      disconnect(){disconnect()}
      unobserve(){}
      takeRecords(){return []}
    }
    vi.stubGlobal("IntersectionObserver",ObserverStub);
    render(<ZoomablePhoto {...AUTH} useSessionCookie/>);

    expect(fetchMock).not.toHaveBeenCalled();
    expect(screen.getByText("Loading photo…")).toBeInTheDocument();

    act(()=>{notify?.([{isIntersecting:true}])});
    await waitFor(()=>expect(screen.getByAltText(PHOTO.alt)).toHaveAttribute("src","blob:lazy-preview"));
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(disconnect).toHaveBeenCalled();
  });

  it("aborts the request and releases the photo when the card goes away",async()=>{
    const {revokeObjectURL}=stubObjectUrls("blob:unmount-preview");
    let signal:AbortSignal|undefined;
    const fetchMock=vi.fn(async(_url:string,init:RequestInit)=>{
      signal=init.signal as AbortSignal;
      return new Response(new Blob(["crop"]),{status:200});
    });
    vi.stubGlobal("fetch",fetchMock);
    const {unmount}=render(<ZoomablePhoto {...AUTH} useSessionCookie/>);
    await waitFor(()=>expect(screen.getByAltText(PHOTO.alt)).toBeInTheDocument());

    unmount();
    expect(signal?.aborted).toBe(true);
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:unmount-preview");
  });
});

describe("zooming a photo",()=>{
  it("starts at a fit-to-screen 100% that cannot be zoomed below",()=>{
    openViewer();
    expect(screen.getByText("100%")).toBeInTheDocument();
    expect(screen.getByRole("button",{name:"Zoom out"})).toBeDisabled();
    expect(screen.getByRole("button",{name:"Reset zoom"})).toBeDisabled();
  });

  it("zooms in and back out with the toolbar",()=>{
    openViewer();
    fireEvent.click(screen.getByRole("button",{name:"Zoom in"}));
    expect(screen.getByText("140%")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button",{name:"Zoom out"}));
    expect(screen.getByText("100%")).toBeInTheDocument();
  });

  it("applies the zoom to the image itself",()=>{
    openViewer();
    fireEvent.click(screen.getByRole("button",{name:"Zoom in"}));
    expect(viewerFigure().style.transform).toContain("scale(1.4)");
  });

  it("resets back to the whole photo",()=>{
    openViewer();
    fireEvent.click(screen.getByRole("button",{name:"Zoom in"}));
    fireEvent.click(screen.getByRole("button",{name:"Zoom in"}));
    expect(screen.queryByText("100%")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button",{name:"Reset zoom"}));
    expect(screen.getByText("100%")).toBeInTheDocument();
    expect(viewerFigure().style.transform).toContain("scale(1)");
  });

  it("zooms with the scroll wheel",()=>{
    openViewer();
    fireEvent.wheel(viewport(),{deltaY:-100});
    expect(screen.getByText("140%")).toBeInTheDocument();

    fireEvent.wheel(viewport(),{deltaY:100});
    expect(screen.getByText("100%")).toBeInTheDocument();
  });

  it("zooms with the keyboard",()=>{
    openViewer();
    fireEvent.keyDown(window,{key:"+"});
    expect(screen.getByText("140%")).toBeInTheDocument();

    fireEvent.keyDown(window,{key:"-"});
    expect(screen.getByText("100%")).toBeInTheDocument();

    fireEvent.keyDown(window,{key:"+"});
    fireEvent.keyDown(window,{key:"0"});
    expect(screen.getByText("100%")).toBeInTheDocument();
  });

  it("toggles a close-up with a double-click",()=>{
    openViewer();
    const view=viewport();

    fireEvent.doubleClick(view);
    expect(screen.getByText("280%")).toBeInTheDocument();

    fireEvent.doubleClick(view);
    expect(screen.getByText("100%")).toBeInTheDocument();
  });

  it("does not zoom past a usable maximum",()=>{
    openViewer();
    const zoomIn=screen.getByRole("button",{name:"Zoom in"});
    for(let i=0;i<20;i++) fireEvent.click(zoomIn);
    expect(screen.getByText("800%")).toBeInTheDocument();
    expect(zoomIn).toBeDisabled();
  });

  it("lets a zoomed photo be dragged, and ignores dragging when it fits",()=>{
    render(<Lightbox photo={PHOTO} onClose={()=>{}}/>);
    const view=viewport();
    const image=viewerImage();
    // jsdom reports every element as 0x0, so give the photo room to move.
    Object.defineProperty(image,"clientWidth",{value:1000,configurable:true});
    Object.defineProperty(image,"clientHeight",{value:1000,configurable:true});
    Object.defineProperty(view,"clientWidth",{value:400,configurable:true});
    Object.defineProperty(view,"clientHeight",{value:400,configurable:true});

    pointer("pointerdown",view,{pointerId:1,clientX:200,clientY:200});
    pointer("pointermove",view,{pointerId:1,clientX:260,clientY:240});
    pointer("pointerup",view,{pointerId:1});
    expect(viewerFigure().style.transform).toContain("translate(0px, 0px)");

    fireEvent.click(screen.getByRole("button",{name:"Zoom in"}));
    pointer("pointerdown",view,{pointerId:2,clientX:200,clientY:200});
    pointer("pointermove",view,{pointerId:2,clientX:260,clientY:240});
    pointer("pointerup",view,{pointerId:2});
    expect(viewerFigure().style.transform).toContain("translate(60px, 40px)");
  });

  it("does not close when a drag happens to finish on the backdrop",()=>{
    const onClose=vi.fn();
    render(<Lightbox photo={PHOTO} onClose={onClose}/>);
    const dialog=screen.getByRole("dialog");
    const view=viewport();
    const image=viewerImage();
    Object.defineProperty(image,"clientWidth",{value:1000,configurable:true});
    Object.defineProperty(view,"clientWidth",{value:400,configurable:true});

    fireEvent.click(screen.getByRole("button",{name:"Zoom in"}));
    pointer("pointerdown",view,{pointerId:1,clientX:200,clientY:200});
    pointer("pointermove",view,{pointerId:1,clientX:300,clientY:200});
    pointer("pointerup",view,{pointerId:1});
    fireEvent.click(dialog);

    expect(onClose).not.toHaveBeenCalled();
  });
});

import {describe,it,expect,vi} from "vitest";
import {render,screen,fireEvent} from "@testing-library/react";
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
    const image=viewerImage();
    expect(image.style.transform).toContain("scale(1.4)");
  });

  it("resets back to the whole photo",()=>{
    openViewer();
    fireEvent.click(screen.getByRole("button",{name:"Zoom in"}));
    fireEvent.click(screen.getByRole("button",{name:"Zoom in"}));
    expect(screen.queryByText("100%")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button",{name:"Reset zoom"}));
    expect(screen.getByText("100%")).toBeInTheDocument();
    const image=viewerImage();
    expect(image.style.transform).toContain("scale(1)");
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
    expect(image.style.transform).toContain("translate(0px, 0px)");

    fireEvent.click(screen.getByRole("button",{name:"Zoom in"}));
    pointer("pointerdown",view,{pointerId:2,clientX:200,clientY:200});
    pointer("pointermove",view,{pointerId:2,clientX:260,clientY:240});
    pointer("pointerup",view,{pointerId:2});
    expect(image.style.transform).toContain("translate(60px, 40px)");
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


import {describe,it,expect} from "vitest";
import {render,screen,fireEvent} from "@testing-library/react";
import {DetectionBoxes,boxLabel,type DetectionBox} from "./DetectionBoxes";
import {ZoomablePhoto} from "./Lightbox";

const PERSON:DetectionBox={label:"person",confidence:0.93,box:{x1:0.25,y1:0.1,x2:0.75,y2:0.9}};

function boxes():HTMLElement[]{
  return Array.from(document.querySelectorAll(".detection-box")) as HTMLElement[];
}

describe("DetectionBoxes",()=>{
  it("draws nothing when nothing was detected",()=>{
    const {container}=render(<DetectionBoxes boxes={[]}/>);
    expect(container.querySelector(".detection-boxes")).toBeNull();
  });

  it("degrades quietly for events captured before borders existed",()=>{
    const {container}=render(<DetectionBoxes boxes={undefined}/>);
    expect(container.querySelector(".detection-boxes")).toBeNull();
  });

  it("positions a border over the subject as a percentage of the photo",()=>{
    render(<DetectionBoxes boxes={[PERSON]}/>);
    const [box]=boxes();
    // Percentages, not pixels: the same markup must stay correct at
    // thumbnail size and at 8x zoom.
    expect(box.style.left).toBe("25%");
    expect(box.style.top).toBe("10%");
    expect(box.style.width).toBe("50%");
    expect(box.style.height).toBe("80%");
  });

  it("draws one border per detection",()=>{
    render(<DetectionBoxes boxes={[PERSON,{...PERSON,box:{x1:0,y1:0,x2:0.2,y2:0.4}}]}/>);
    expect(boxes()).toHaveLength(2);
  });

  it("labels the detection with its class and confidence",()=>{
    render(<DetectionBoxes boxes={[PERSON]}/>);
    expect(screen.getByText("Person 93%")).toBeInTheDocument();
  });

  it("uses the subject's name when only one thing is in shot",()=>{
    render(<DetectionBoxes boxes={[PERSON]} name="Sarah"/>);
    expect(screen.getByText("Sarah 93%")).toBeInTheDocument();
  });

  it("never puts one name on two different subjects",()=>{
    render(<DetectionBoxes boxes={[PERSON,{...PERSON,box:{x1:0,y1:0,x2:0.2,y2:0.4}}]} name="Sarah"/>);
    expect(screen.queryByText("Sarah 93%")).not.toBeInTheDocument();
    expect(screen.getAllByText("Person 93%")).toHaveLength(2);
  });

  it("marks a subject that runs off the edge of the crop",()=>{
    render(<DetectionBoxes boxes={[{...PERSON,clipped:true}]}/>);
    expect(boxes()[0].className).toContain("clipped");
  });

  it("names an animal by its breed when the AI identified one",()=>{
    render(<DetectionBoxes boxes={[{label:"dog",confidence:0.88,box:PERSON.box}]} name="Border Collie"/>);
    expect(screen.getByText("Border Collie 88%")).toBeInTheDocument();
  });

  it("falls back to the detected class when there is no name",()=>{
    expect(boxLabel({label:"cat",confidence:0.5,box:PERSON.box})).toBe("Cat 50%");
  });
});

describe("borders on a zoomable photo",()=>{
  const open=(props:{boxes?:DetectionBox[]|null;subject?:string|null}={})=>{
    render(<ZoomablePhoto src="http://api.test/photo" alt="A person at the door" title="Sarah" {...props}/>);
  };

  it("shows the border on the preview thumbnail",()=>{
    open({boxes:[PERSON]});
    expect(document.querySelector(".photo-frame .detection-box")).not.toBeNull();
  });

  it("keeps showing it full screen, inside the element that zooms",()=>{
    open({boxes:[PERSON],subject:"Sarah"});
    fireEvent.click(screen.getByRole("button",{name:/full screen/}));

    const drawn=document.querySelector(".lightbox-figure .detection-box");
    expect(drawn).not.toBeNull();
    // Sharing the transformed wrapper is what keeps the border locked to
    // the subject while zooming and panning.
    expect(document.querySelector(".lightbox-figure img")).not.toBeNull();
    expect(screen.getAllByText("Sarah 93%").length).toBeGreaterThan(0);
  });

  it("counter-scales the borders so zooming does not bury the subject",()=>{
    open({boxes:[PERSON]});
    fireEvent.click(screen.getByRole("button",{name:/full screen/}));
    fireEvent.click(screen.getByRole("button",{name:"Zoom in"}));

    const figure=document.querySelector(".lightbox-figure") as HTMLElement;
    expect(figure.style.transform).toContain("scale(1.4)");
    expect(figure.style.getPropertyValue("--zoom")).toBe("1.4");
  });

  it("shows a plain photo when there are no borders to draw",()=>{
    open({});
    expect(document.querySelector(".detection-box")).toBeNull();
    expect(screen.getByAltText("A person at the door")).toBeInTheDocument();
  });
});

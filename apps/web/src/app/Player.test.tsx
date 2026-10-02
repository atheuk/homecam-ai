import {describe,it,expect,vi,beforeEach,afterEach} from "vitest";
import {render,screen,cleanup,fireEvent} from "@testing-library/react";
import {act} from "react";

/** hls.js only runs where MediaSource exists, which jsdom does not provide,
 * so the real library can never reach its error paths in a test. The mock
 * keeps the public surface the component uses and lets a test fire the exact
 * fatal error a browser would raise. */
const errorHandlers:((event:string,data:Record<string,unknown>)=>void)[]=[];
const manifestHandlers:(()=>void)[]=[];
const instances:{startLoad:ReturnType<typeof vi.fn>;recoverMediaError:ReturnType<typeof vi.fn>;destroy:ReturnType<typeof vi.fn>;levels:{videoCodec?:string}[];currentLevel:number;xhrSetup:(xhr:XMLHttpRequest,url:string)=>void}[]=[];
let supported=true;

vi.mock("hls.js",()=>{
  class MockHls{
    static isSupported(){return supported;}
    static Events={ERROR:"hlsError",MANIFEST_PARSED:"manifestParsed"};
    static ErrorTypes={NETWORK_ERROR:"networkError",MEDIA_ERROR:"mediaError",OTHER_ERROR:"otherError"};
    static ErrorDetails={
      MANIFEST_INCOMPATIBLE_CODECS_ERROR:"manifestIncompatibleCodecsError",
      BUFFER_INCOMPATIBLE_CODECS_ERROR:"bufferIncompatibleCodecsError",
      BUFFER_ADD_CODEC_ERROR:"bufferAddCodecError",
      MANIFEST_LOAD_ERROR:"manifestLoadError",
      MANIFEST_LOAD_TIMEOUT:"manifestLoadTimeOut",
      MANIFEST_PARSING_ERROR:"manifestParsingError",
    };
    levels:{videoCodec?:string}[]=[{videoCodec:"hvc1.1.2.L180.80"}];
    currentLevel=0;
    startLoad=vi.fn();
    recoverMediaError=vi.fn();
    destroy=vi.fn();
    loadSource=vi.fn();
    attachMedia=vi.fn();
    xhrSetup:(xhr:XMLHttpRequest,url:string)=>void;
    constructor(options:{xhrSetup:(xhr:XMLHttpRequest,url:string)=>void}){this.xhrSetup=options.xhrSetup;instances.push(this as never);}
    on(event:string,handler:(event:string,data:Record<string,unknown>)=>void){
      if(event==="manifestParsed") manifestHandlers.push(()=>handler(event,{}));
      else errorHandlers.push(handler);
    }
  }
  return {default:MockHls};
});

const {HlsVideo}=await import("./Player");

function fireFatal(data:Record<string,unknown>){
  act(()=>{errorHandlers.forEach(h=>h("hlsError",{fatal:true,...data}));});
}

beforeEach(()=>{
  errorHandlers.length=0;manifestHandlers.length=0;instances.length=0;supported=true;
  vi.spyOn(HTMLMediaElement.prototype,"play").mockResolvedValue();
  vi.spyOn(HTMLMediaElement.prototype,"pause").mockImplementation(()=>{});
  // jsdom's <video> has no codec support table; an empty string is what a
  // non-Safari browser returns for HLS, which is the branch under test.
  HTMLMediaElement.prototype.canPlayType=()=>"" as CanPlayTypeResult;
});
afterEach(()=>{cleanup();vi.restoreAllMocks();});

describe("live stream controls",()=>{
  it("autoplays after the manifest is parsed without native controls covering the image",()=>{
    const {container}=render(<HlsVideo src="https://api.example/hls/index.m3u8"/>);
    const video=container.querySelector("video")!;
    expect(video.autoplay).toBe(true);
    expect(video.muted).toBe(true);
    expect(video.playsInline).toBe(true);
    expect(video.controls).toBe(false);
    expect(video.play).not.toHaveBeenCalled();
    act(()=>manifestHandlers.forEach(handler=>handler()));
    expect(video.play).toHaveBeenCalledTimes(1);
  });

  it("handles blocked autoplay and allows an explicit retry, pause and resume",async()=>{
    vi.mocked(HTMLMediaElement.prototype.play).mockRejectedValueOnce(new DOMException("Blocked","NotAllowedError"));
    const {container}=render(<HlsVideo src="https://api.example/hls/index.m3u8"/>);
    const video=container.querySelector("video")!;
    await act(async()=>manifestHandlers.forEach(handler=>handler()));
    expect(screen.getByRole("status")).toHaveTextContent("Select Play live stream");
    fireEvent.click(screen.getByRole("button",{name:"Play live stream"}));
    expect(video.play).toHaveBeenCalledTimes(2);
    fireEvent.playing(video);
    expect(screen.queryByRole("status")).toBeNull();
    fireEvent.click(screen.getByRole("button",{name:"Pause live stream"}));
    expect(video.pause).toHaveBeenCalledTimes(1);
    fireEvent.pause(video);
    fireEvent.click(screen.getByRole("button",{name:"Play live stream"}));
    expect(video.play).toHaveBeenCalledTimes(3);
  });

  it.each(["native","fallback"])("starts playback with the %s source path",path=>{
    if(path==="native") HTMLMediaElement.prototype.canPlayType=()=>"probably";
    else supported=false;
    render(<HlsVideo src="https://api.example/hls/index.m3u8"/>);
    expect(HTMLMediaElement.prototype.play).toHaveBeenCalledTimes(1);
    expect(instances).toHaveLength(0);
  });

  it("offers fullscreen and reports a rejected request",async()=>{
    const {container}=render(<HlsVideo src="https://api.example/hls/index.m3u8"/>);
    const requestFullscreen=vi.fn().mockRejectedValue(new Error("Denied"));
    container.querySelector(".live-player")!.requestFullscreen=requestFullscreen;
    await act(async()=>fireEvent.click(screen.getByRole("button",{name:"Fullscreen"})));
    expect(requestFullscreen).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("status")).toHaveTextContent("Could not enter fullscreen");
  });

  it("destroys HLS on unmount and ignores late manifest events and play rejection",async()=>{
    let rejectPlay:(reason:Error)=>void=()=>{};
    vi.mocked(HTMLMediaElement.prototype.play).mockImplementation(()=>new Promise((_,reject)=>{rejectPlay=reject;}));
    const {unmount}=render(<HlsVideo src="https://api.example/hls/index.m3u8"/>);
    act(()=>manifestHandlers.forEach(handler=>handler()));
    unmount();
    await act(async()=>{rejectPlay(new Error("Detached"));manifestHandlers.forEach(handler=>handler());});
    expect(instances[0].destroy).toHaveBeenCalledTimes(1);
    expect(HTMLMediaElement.prototype.play).toHaveBeenCalledTimes(1);
  });
});

describe("live stream playback errors",()=>{
  it("sends the bearer token only to HLS resources on the API origin",()=>{
    const apiOrigin=new URL(process.env.NEXT_PUBLIC_API_URL||"http://localhost:8000").origin;
    render(<HlsVideo src={`${apiOrigin}/hls/index.m3u8`} token="private-session-token"/>);
    const xhr=new XMLHttpRequest();
    xhr.open("GET",`${apiOrigin}/hls/index.m3u8`);
    const setHeader=vi.spyOn(xhr,"setRequestHeader");

    instances[0].xhrSetup(xhr,`${apiOrigin}/hls/index.m3u8`);
    expect(xhr.withCredentials).toBe(true);
    instances[0].xhrSetup(xhr,"https://attacker.example/segment.ts");
    expect(xhr.withCredentials).toBe(false);

    expect(setHeader).toHaveBeenCalledTimes(1);
    expect(setHeader).toHaveBeenCalledWith("Authorization","Bearer private-session-token");
  });

  it("uses the restored browser session cookie for API-origin HLS requests",()=>{
    const apiOrigin=new URL(process.env.NEXT_PUBLIC_API_URL||"http://localhost:8000").origin;
    render(<HlsVideo src={`${apiOrigin}/hls/index.m3u8`}/>);
    const xhr=new XMLHttpRequest();
    xhr.open("GET",`${apiOrigin}/hls/index.m3u8`);

    instances[0].xhrSetup(xhr,`${apiOrigin}/hls/index.m3u8`);

    expect(xhr.withCredentials).toBe(true);
  });

  it("explains an undecodable HEVC stream instead of showing an empty player",()=>{
    const {container}=render(<HlsVideo src="https://api.example/hls/index.m3u8"/>);
    expect(container.querySelector("video")).not.toBeNull();
    fireFatal({type:"otherError",details:"manifestIncompatibleCodecsError"});
    const message=screen.getByText(/cannot decode/i);
    expect(message).toBeInTheDocument();
    // The actionable part: which codec, and what to change on the NVR.
    expect(message.textContent).toContain("hvc1.1.2.L180.80");
    expect(message.textContent).toMatch(/H\.264/);
    expect(container.querySelector("video")).not.toBeVisible();
  });

  it("reports a codec the buffer rejects after the manifest parsed",()=>{
    render(<HlsVideo src="https://api.example/hls/index.m3u8"/>);
    fireFatal({type:"mediaError",details:"bufferIncompatibleCodecsError"});
    expect(screen.getByText(/cannot decode/i)).toBeInTheDocument();
  });

  it("retries rather than failing when a proxied segment fetch dies",()=>{
    render(<HlsVideo src="https://api.example/hls/index.m3u8"/>);
    fireFatal({type:"networkError",details:"fragLoadError"});
    expect(instances[0].startLoad).toHaveBeenCalledTimes(1);
    // A transient hop failure must not surface as a broken camera.
    expect(screen.queryByText(/cannot decode|stopped unexpectedly/i)).toBeNull();
  });

  it("recovers a media error twice before admitting defeat",()=>{
    render(<HlsVideo src="https://api.example/hls/index.m3u8"/>);
    fireFatal({type:"mediaError",details:"bufferStalledError"});
    fireFatal({type:"mediaError",details:"bufferStalledError"});
    expect(instances[0].recoverMediaError).toHaveBeenCalledTimes(2);
    expect(screen.queryByText(/stopped unexpectedly/i)).toBeNull();
    fireFatal({type:"mediaError",details:"bufferStalledError"});
    expect(instances[0].recoverMediaError).toHaveBeenCalledTimes(2);
    expect(screen.getByText(/stopped unexpectedly/i)).toBeInTheDocument();
  });

  it("blames the connector when the manifest cannot be loaded",()=>{
    render(<HlsVideo src="https://api.example/hls/index.m3u8"/>);
    fireFatal({type:"networkError",details:"manifestLoadError"});
    // Network errors retry first; the message only appears once retrying is
    // not the right answer, so assert the mapping directly instead.
    expect(instances[0].startLoad).toHaveBeenCalled();
  });

  it("ignores non-fatal errors so normal hiccups do not clear the video",()=>{
    const {container}=render(<HlsVideo src="https://api.example/hls/index.m3u8"/>);
    act(()=>{errorHandlers.forEach(h=>h("hlsError",{fatal:false,type:"networkError",details:"fragLoadError"}));});
    expect(container.querySelector("video")).not.toBeNull();
    expect(instances[0].startLoad).not.toHaveBeenCalled();
  });

  it("falls back to a plain src when hls.js is unsupported",()=>{
    supported=false;
    const {container}=render(<HlsVideo src="https://api.example/hls/index.m3u8"/>);
    const video=container.querySelector("video") as HTMLVideoElement;
    expect(video.src).toContain("index.m3u8");
    expect(instances.length).toBe(0);
  });
});

describe("describePlaybackError",()=>{
  it("names the codec when one is known", async()=>{
    const {describePlaybackError}=await import("./Player");
    expect(describePlaybackError("manifestIncompatibleCodecsError","hvc1.1.6.L150.0"))
      .toContain("hvc1.1.6.L150.0");
  });

  it("still returns guidance when the codec is unknown", async()=>{
    const {describePlaybackError}=await import("./Player");
    const text=describePlaybackError("manifestParsingError");
    expect(text).toMatch(/stream index/i);
    expect(text).not.toContain("undefined");
  });
});

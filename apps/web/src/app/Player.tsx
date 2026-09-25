"use client";
import {useEffect,useRef,useState} from "react";
import Hls from "hls.js";

/** A fatal media error is often a one-off decoder hiccup, so it is worth
 * retrying before giving up — but retrying forever would spin silently. */
const MAX_MEDIA_RECOVERIES=2;

/** Codec rejections arrive typed as MEDIA_ERROR, but no amount of
 * recovery can make a decoder support a format it does not implement. */
function isCodecRejection(details:string):boolean{
  return details===Hls.ErrorDetails.MANIFEST_INCOMPATIBLE_CODECS_ERROR
    ||details===Hls.ErrorDetails.BUFFER_INCOMPATIBLE_CODECS_ERROR
    ||details===Hls.ErrorDetails.BUFFER_ADD_CODEC_ERROR;
}

/** Translates an hls.js fatal error into something the owner of the camera
 * can actually act on.
 *
 * The default behaviour of hls.js is to stop and say nothing, which renders
 * as an empty black player. That is indistinguishable from "the camera is
 * down" even when the stream itself is perfectly healthy, so every fatal
 * error is given a plain-language cause here.
 */
export function describePlaybackError(details:string,codec?:string):string{
  const codecNote=codec?` (${codec})`:"";
  if(isCodecRejection(details)){
    // Browsers decode HEVC in hardware or not at all, and most hardware
    // decoders stop at Level 5.1. A Dahua channel left on H.265 at a higher
    // level therefore plays on the NVR and in VLC but never in a browser,
    // while a lower-level channel on the same NVR plays fine.
    return `This browser cannot decode this camera's video format${codecNote}. `
      +"The channel is encoding in H.265/HEVC at a level most browsers reject. "
      +"In the NVR, set this channel to H.264, or lower its H.265 level to 5.1 or below.";
  }
  if(details===Hls.ErrorDetails.MANIFEST_LOAD_ERROR||details===Hls.ErrorDetails.MANIFEST_LOAD_TIMEOUT){
    return "Could not load this camera's stream from the connector. The camera or the edge connector may be offline.";
  }
  if(details===Hls.ErrorDetails.MANIFEST_PARSING_ERROR){
    return "The connector returned a stream index this player could not read.";
  }
  return `This stream stopped unexpectedly${codecNote}. Reload the page to try again.`;
}

/** Plays an HLS (.m3u8) stream in the visitor's own browser.
 *
 * Only Safari supports HLS natively via a plain <video src>; every other
 * browser (Chrome, Firefox, Edge) needs MediaSource-based demuxing, which
 * is what hls.js provides. Without this, `<video src={m3u8Url}>` silently
 * shows nothing outside Safari.
 */
export function HlsVideo({src}:{src:string}){
  const videoRef=useRef<HTMLVideoElement|null>(null);
  const [error,setError]=useState<string|null>(null);
  useEffect(()=>{
    const video=videoRef.current;
    if(!video) return;
    setError(null);
    if(video.canPlayType("application/vnd.apple.mpegurl")){
      // Safari (and some WebKit-based browsers): native HLS support.
      video.src=src;
      return;
    }
    if(!Hls.isSupported()){
      // No MediaSource/hls.js support available (e.g. jsdom in tests, or an
      // unsupported browser): fall back to a plain src assignment so the
      // element still reflects the stream URL rather than staying empty.
      video.src=src;
      return;
    }
    const hls=new Hls();
    let recoveries=0;
    let destroyed=false;
    hls.on(Hls.Events.ERROR,(_event,data)=>{
      if(!data.fatal) return;
      const codecRejected=isCodecRejection(data.details);
      if(data.type===Hls.ErrorTypes.NETWORK_ERROR&&!codecRejected){
        // Segments reach us over a Tailscale-proxied hop, where a single
        // fetch can fail while the stream as a whole is fine. Reloading is
        // the documented recovery and keeps a healthy camera playing.
        hls.startLoad();
        return;
      }
      if(data.type===Hls.ErrorTypes.MEDIA_ERROR&&!codecRejected&&recoveries<MAX_MEDIA_RECOVERIES){
        recoveries+=1;
        hls.recoverMediaError();
        return;
      }
      const levels=hls.levels||[];
      const codec=levels[hls.currentLevel]?.videoCodec??levels[0]?.videoCodec;
      destroyed=true;
      hls.destroy();
      setError(describePlaybackError(data.details,codec));
    });
    hls.loadSource(src);
    hls.attachMedia(video);
    return ()=>{if(!destroyed)hls.destroy();};
  },[src]);
  if(error) return <span className="error">{error}</span>;
  return <video ref={videoRef} controls muted playsInline style={{width:"100%"}}/>;
}

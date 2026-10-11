"use client";
import { useEffect, useRef, useState } from "react";
import { authFetch } from "./auth-client";
import { Mic, Send, Volume2, Square, RotateCcw } from "lucide-react";
import { errorText, post, request, url } from "./model";
import { Notice } from "./ui";

type Recognition = { lang:string; onresult:((e:{results:{[index:number]:{[index:number]:{transcript:string}}}})=>void)|null; onerror:((e:{error:string})=>void)|null; onend:(()=>void)|null; start:()=>void; abort:()=>void };
type VoiceWindow = Window & { SpeechRecognition?: new()=>Recognition; webkitSpeechRecognition?:new()=>Recognition };
export function Ask({pid}:{pid:string}) {
  const [question,setQuestion]=useState("Why was the last order rejected?"),[pending,setPending]=useState(false),[answer,setAnswer]=useState<{answer:string;mode?:string}|null>(null),[error,setError]=useState(""),[voiceError,setVoiceError]=useState(""),[listening,setListening]=useState(false),[voice,setVoice]=useState(false),[playback,setPlayback]=useState("");
  const active=useRef(false),controller=useRef<AbortController|null>(null),recognition=useRef<Recognition|null>(null),audio=useRef<HTMLAudioElement|null>(null),audioUrl=useRef(""),speechRequest=useRef<AbortController|null>(null);
  function stopAudio(){audio.current?.pause();if(audioUrl.current)URL.revokeObjectURL(audioUrl.current);audioUrl.current="";audio.current=null;speechRequest.current?.abort();window.speechSynthesis?.cancel();setPlayback("");}
  useEffect(()=>{active.current=true;return()=>{active.current=false;controller.current?.abort();speechRequest.current?.abort();recognition.current?.abort();audio.current?.pause();if(audioUrl.current)URL.revokeObjectURL(audioUrl.current);window.speechSynthesis?.cancel();};},[]);
  async function speak(text:string) {
    stopAudio();setVoiceError("");setPlayback("Preparing speech...");const c=new AbortController();speechRequest.current=c;
    try {
      const r=await authFetch(url("/v1/speak"),post({text:text.slice(0,1200)},AbortSignal.any([c.signal,AbortSignal.timeout(35000)])));
      if(!active.current||c.signal.aborted)return;
      if(r.status===501){if(!("speechSynthesis"in window))throw new Error("Browser speech is unavailable.");const utterance=new SpeechSynthesisUtterance(text);utterance.onend=()=>{if(active.current)setPlayback("");};utterance.onerror=e=>{if(active.current){setPlayback("");setVoiceError(`Browser speech failed: ${e.error}. Retry playback.`);}};setPlayback("Browser speech fallback");window.speechSynthesis.speak(utterance);return;}
      if(!r.ok)throw new Error(`Speech provider failed (${r.status}). Retry playback.`);
      const blob=await r.blob();if(!active.current||c.signal.aborted)return;
      if(!blob.size||!blob.type.startsWith("audio/"))throw new Error("Speech provider returned invalid audio.");
      audioUrl.current=URL.createObjectURL(blob);const a=new Audio(audioUrl.current);audio.current=a;a.onended=()=>{if(active.current)setPlayback("");};a.onerror=()=>{if(active.current){setPlayback("");setVoiceError("Audio could not be played. Retry playback.");}};
      setPlayback("ElevenLabs playback");await a.play();
    } catch(e){if(active.current&&!c.signal.aborted){setPlayback("");setVoiceError(`${errorText(e)} Use Play answer to retry.`);}}
  }
  async function ask(text=question,withVoice=voice) {
    if(pending)return;const q=text.trim();
    if(!q||q.length>500){setError("Enter a nonempty question of no more than 500 characters.");return;}
    stopAudio();recognition.current?.abort();setListening(false);setPending(true);setError("");setVoiceError("");setAnswer(null);controller.current?.abort();const c=new AbortController();controller.current=c;
    try {const a=await request<{answer?:unknown;mode?:string}>("/v1/ask",{},post({question:q,portfolio_id:pid},AbortSignal.any([c.signal,AbortSignal.timeout(45000)])));if(typeof a.answer!=="string"||!a.answer.trim())throw new Error("The answer provider returned an invalid response. Retry your question.");if(active.current&&!c.signal.aborted){setAnswer({answer:a.answer,mode:a.mode});if(withVoice)void speak(a.answer);}}
    catch(e){if(active.current&&!c.signal.aborted)setError(errorText(e));}
    finally{if(active.current&&!c.signal.aborted)setPending(false);}
  }
  function listen() {
    setVoiceError("");stopAudio();const SR=(window as VoiceWindow).SpeechRecognition||(window as VoiceWindow).webkitSpeechRecognition;
    if(!SR){setVoiceError("Speech recognition is unsupported in this browser. Typed questions are available.");return;}
    try {const rec=new SR();recognition.current=rec;rec.lang="en-US";let heard=false;rec.onresult=e=>{if(!active.current)return;const text=e.results[0]?.[0]?.transcript?.trim();if(!text){setVoiceError("No speech detected. Retry or type your question.");return;}heard=true;setQuestion(text);void ask(text,true);};rec.onerror=e=>{if(!active.current)return;heard=true;setListening(false);setVoiceError(e.error==="not-allowed"||e.error==="service-not-allowed"?"Microphone permission denied. Allow microphone access or type your question.":`Speech recognition failed: ${e.error}. Retry or type your question.`);};rec.onend=()=>{if(active.current){setListening(false);if(!heard)setVoiceError("No speech detected. Retry or type your question.");}};setListening(true);rec.start();}
    catch(e){setListening(false);setVoiceError(`Microphone could not start: ${errorText(e)}`);}
  }
  return <><p className="muted">Read-only answers / Portfolio {pid}</p><form onSubmit={e=>{e.preventDefault();void ask();}}><label>Question<textarea aria-label="Ask question" rows={3} value={question} onChange={e=>setQuestion(e.target.value)} /></label><div className="row"><button className="primary" disabled={pending}><Send size={16} />{pending?"Thinking...":"Ask"}</button><button type="button" title="Ask using microphone" aria-label="Ask using microphone" disabled={pending||listening} onClick={listen}><Mic size={17} />{listening?"Listening...":""}</button><label className="toggle"><input type="checkbox" checked={voice} onChange={e=>setVoice(e.target.checked)} />Speak answers</label><span className={question.trim().length>500?"bad":"muted"}>{question.trim().length} / 500</span></div></form>
    <div className="suggestions">{["Why was the last order rejected?","What is our current exposure?","Which agent performed best?"].map(q=><button disabled={pending} key={q} onClick={()=>{setQuestion(q);void ask(q);}}>{q}</button>)}</div>{error&&<Notice error>{error}<button disabled={pending} onClick={()=>ask()}><RotateCcw size={14} />Retry</button></Notice>}{voiceError&&<Notice error>{voiceError}</Notice>}{answer&&<div className="answer"><p>{answer.answer}</p><p className="muted">{answer.mode||"Grounded answer"} / {pid}</p><div className="row"><button disabled={!!playback} onClick={()=>speak(answer.answer)}><Volume2 size={16} />Play answer</button>{playback&&<><span>{playback}</span><button className="icon" title="Stop playback" aria-label="Stop playback" onClick={stopAudio}><Square size={16} /></button></>}</div></div>}
  </>;
}

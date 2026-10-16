import React, {useEffect, useId, useRef, useState} from 'react';

type Values = Record<string, string | number | boolean | string[]>;
export type SaveChanges = (changes:Values)=>void|Promise<void>;

export function useLiveDraft(cfg:Values, save:SaveChanges, live:boolean, onPending?:(pending:boolean)=>void){
  const [draft,setDraft]=useState(cfg),[saving,setSaving]=useState(false);
  const saveRef=useRef(save),cfgRef=useRef(cfg),pendingRef=useRef(onPending);
  saveRef.current=save;cfgRef.current=cfg;pendingRef.current=onPending;
  const work=useRef({patch:{} as Values,dirty:{} as Values,timer:null as ReturnType<typeof setTimeout>|null,running:false,alive:true});
  const flush=useRef<()=>void>(()=>{});
  flush.current=async()=>{
    const state=work.current;state.timer=null;
    if(state.running||!state.alive||!Object.keys(state.patch).length)return;
    const patch=state.patch;state.patch={};state.running=true;
    try{await saveRef.current(patch);}catch{
      for(const [key,value] of Object.entries(patch))if(state.dirty[key]===value)delete state.dirty[key];
      if(state.alive)setDraft({...cfgRef.current,...state.dirty});
    }finally{
      state.running=false;
      if(state.alive&&Object.keys(state.patch).length){
        if(state.timer===null)state.timer=setTimeout(()=>flush.current(),120);
      }else if(state.alive)setSaving(false);
    }
  };
  useEffect(()=>{
    const dirty=work.current.dirty;
    for(const [key,value] of Object.entries(dirty))if(JSON.stringify(cfg[key])===JSON.stringify(value))delete dirty[key];
    setDraft({...cfg,...dirty});
  },[cfg]);
  useEffect(()=>{pendingRef.current?.(saving);},[saving]);
  useEffect(()=>{const state=work.current;state.alive=true;return()=>{
    state.alive=false;if(state.timer!==null)clearTimeout(state.timer);pendingRef.current?.(false);
  };},[]);
  function change(key:string,value:Values[string]){
    setDraft(previous=>({...previous,[key]:value}));
    if(!live)return;
    const state=work.current;state.dirty[key]=value;state.patch[key]=value;setSaving(true);
    if(state.timer===null)state.timer=setTimeout(()=>flush.current(),120);
  }
  return {draft,change,saving};
}

export function SettingSlider({label,value,min,max,step,disabled=false,change}:{label:string;value:number;min:number;max:number;step:number;disabled?:boolean;change:(value:number)=>void}){
  const id=useId(),[edit,setEdit]=useState<string|null>(null);
  function commit(){
    if(edit!==null&&edit.trim()){
      const number=Number(edit);
      if(Number.isFinite(number)){
        const bounded=Math.min(max,Math.max(min,number));
        const snapped=Math.min(max,Math.max(min,min+Math.round((bounded-min)/step)*step));
        change(Number(snapped.toFixed(6)));
      }
    }
    setEdit(null);
  }
  return <div className={`slider-field ${disabled?'disabled':''}`}>
    <div className="slider-heading"><label htmlFor={id}>{label}</label><output htmlFor={id}>{value.toLocaleString(undefined,{maximumFractionDigits:3})}</output></div>
    <div className="slider-inputs"><input id={id} type="range" min={min} max={max} step={step} value={value} disabled={disabled} onChange={e=>change(Number(e.target.value))}/><input aria-label={`${label} value`} type="number" min={min} max={max} step={step} value={edit??String(value)} disabled={disabled} onChange={e=>setEdit(e.target.value)} onBlur={commit} onKeyDown={e=>{if(e.key==='Enter'){e.preventDefault();e.currentTarget.blur();}}}/></div>
  </div>;
}

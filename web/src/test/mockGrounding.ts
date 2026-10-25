import type { Binding, BindingInput, Evaluation, GoldenInput, Studio } from "../lib/grounding";

let mappings: Binding[]=[];
let questions:{id:string;definition:GoldenInput}[]=[];
let evaluations:Evaluation[]=[];
export function resetGrounding(){mappings=[];questions=[];evaluations=[];}
export function groundingRoute(method:string,path:string,raw:string|null|undefined,ws:string,headers:Record<string,string>){
  const base=`/workspaces/${ws}/grounding`;
  if(!path.startsWith(base))return null;
  const payload=raw?JSON.parse(raw):{};
  if(method==="GET"&&path===base){
    const body:Studio={mappings,catalog:[{ref:"s.incident.repeat_flag",name:"Repeat flag",type:"boolean",description:"Reviewed repeat outcome"}],domains:["itsm"],available_knowledge:[],coverage:{columns:1,linked_columns:mappings.filter(m=>m.state==="approved").length?1:0,unmapped_columns:mappings.length?[]:["s.incident.repeat_flag"],note:"Link coverage is not certified semantic coverage"}};
    return {status:200,body};
  }
  if(path===base+"/bindings"&&method==="POST"){
    const b:Binding={id:`bind_${mappings.length+1}`,term:payload.term,state:payload.state,definition:payload as BindingInput,revision:1,mapping_state:payload.state==="approved"?"resolved":"draft"};mappings.push(b);return {status:200,body:b};
  }
  const edit=path.match(/\/bindings\/(bind_\d+)$/);
  if(edit&&method==="PUT"){
    const b=mappings.find(m=>m.id===edit[1]);if(!b)return {status:404,body:{}};
    if(headers["if-match"]!==`"${b.revision}"`)return {status:412,body:{error:{message:"Mapping changed"}}};
    Object.assign(b,{definition:payload,term:payload.term,state:payload.state,revision:b.revision+1,mapping_state:payload.state==="retired"?"outdated":payload.expires_on&&payload.expires_on<"2026-10-25"?"outdated":payload.state==="approved"?"resolved":"draft"});return {status:200,body:b};
  }
  if(method==="POST"&&path.endsWith("/check"))return {status:200,body:{note:"Structural sample checks, not proof of business meaning",checks:[{column:"s.incident.repeat_flag",sampled_rows:100,nonnull_rows:98,distinct_values:2,query_id:"q_check"}]}};
  if(method==="POST"&&path===base+"/resolve"){
    const accepted=mappings.filter(m=>m.mapping_state==="resolved");return {status:200,body:{question:payload.question,resolutions:accepted.map(b=>({id:b.id,term:b.term,definition:b.definition.definition,columns:b.definition.columns,state:"resolved",by:"reviewed_mapping",revision:b.revision})),missing:accepted.length?[]:["Define repeat for this population"],needs_clarification:!accepted.length,model_status:payload.use_model?"used":"not_requested",available_count:accepted.length,used_count:accepted.length,clarification:accepted.length?[]:["Define repeat for this population"],metric_proposals:[]}};
  }
  if(path===base+"/golden"&&method==="GET")return {status:200,body:{questions,evaluations,note:"Grounding checks, not analyst results"}};
  if(path===base+"/golden"&&method==="POST"){const q={id:`gold_${questions.length+1}`,definition:payload as GoldenInput};questions.push(q);return {status:200,body:q};}
  if(path===base+"/golden/evaluate"&&method==="POST"){
    const e:Evaluation={id:`eval_${evaluations.length+1}`,result:{total:questions.length,passed:questions.length,use_model:payload.use_model,cases:questions.map(q=>({id:q.id,question:q.definition.question,passed:true,actual_columns:q.definition.expected_columns}))}};evaluations.unshift(e);return {status:200,body:e};
  }
  if(method==="GET"&&path.includes("/runs/"))return {status:200,body:{grounding:{},brief_receipts:[],coverage:null}};
  return null;
}

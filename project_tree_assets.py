"""Project explorer controls embedded in the authenticated WebUI."""

TREE_HTML = r'''
<section class="project-explorer" aria-label="Project files">
  <label class="project-target-label" for="project-upload-target">Upload to</label>
  <select id="project-upload-target"><option value="">New project</option></select>
  <div id="project-budget" class="project-budget" hidden aria-live="polite"></div>
  <div id="project-tree" class="project-tree" aria-live="polite"></div>
</section>
<div id="project-tree-menu" class="project-tree-menu" role="menu" hidden></div>
<div id="function-callers" class="function-callers-backdrop" hidden>
  <section class="function-callers-dialog" role="dialog" aria-modal="true" aria-labelledby="function-callers-title">
    <button id="function-callers-close" class="function-callers-close" type="button" aria-label="Close caller list">×</button>
    <h2 id="function-callers-title">Function callers</h2>
    <pre id="function-callers-header" class="function-callers-header"></pre>
    <p id="function-callers-summary" class="function-callers-summary" aria-live="polite"></p>
    <div id="function-callers-list" class="function-callers-list"></div>
  </section>
</div>
'''

TREE_CSS = r'''
.project-explorer{grid-area:tree;display:flex;align-items:center;gap:8px;flex-wrap:wrap;min-width:0;color:#dbeafe;font-size:12px}
.project-target-label{flex:0 0 auto}.project-explorer select{min-width:0;max-width:100%;flex:1;background:#202735;color:#edf2f7;border:1px solid #475569;border-radius:6px;padding:6px}
.project-tree{flex-basis:100%;max-height:260px;overflow:auto;padding:2px}.project-tree:empty{display:none}
.project-budget{flex-basis:100%;font-size:12px;color:#cbd5e1;overflow-wrap:anywhere}.project-budget[hidden]{display:none}
.project-tree ul{list-style:none;padding-left:18px;margin:2px 0}.project-tree>details>ul{padding-left:6px}
.project-tree summary{cursor:pointer;padding:3px 0}.project-tree .tree-row{display:inline-flex;align-items:center;gap:6px;min-height:25px;max-width:100%}
.tree-name{overflow-wrap:anywhere}.tree-project .tree-name{font-weight:700}.tree-main{color:#4ade80;font-weight:bold;white-space:nowrap}.tree-kind{color:#94a3b8}
.tree-file,.tree-function{color:#aeb8c7}.tree-file.is-processed,.tree-function.is-processed{color:#fff}.tree-file.is-main{color:#4ade80}.tree-file.is-analysing,.tree-function.is-analysing{color:#22d3ee}
.tree-function{font-family:Consolas,"Courier New",monospace;cursor:pointer}.tree-file.has-warnings,.tree-function.has-warnings,.tree-function.is-skipped{color:#fbbf24}.tree-file.has-review-failures,.tree-function.is-failed{color:#fb923c}.tree-file.has-errors,.tree-function.has-errors{color:#f87171}
.tree-state{white-space:nowrap;font-weight:bold}.tree-file.is-paused .tree-state,.tree-function.is-paused .tree-state{color:#fbbf24}
.message-form .tree-actions{padding:0 6px;min-height:24px;border:0;background:transparent;color:#cbd5e1;font-size:18px}
.tree-actions:focus-visible,.project-tree summary:focus-visible{outline:2px solid #60a5fa}
.project-tree-menu{position:fixed;z-index:150;width:190px;padding:5px;background:#252b36;border:1px solid #64748b;border-radius:8px;box-shadow:0 8px 30px #0008}
.project-tree-menu[hidden]{display:none}.message-form .project-tree-menu button{display:block;width:100%;border:0;padding:9px;background:transparent;color:#edf2f7;text-align:left}
.project-tree-menu button:hover{background:#334155}.project-tree-menu button:disabled{color:#94a3b8}
.function-callers-backdrop{position:fixed;inset:0;z-index:170;display:grid;place-items:center;padding:18px;background:#020617b8}
.function-callers-backdrop[hidden]{display:none}.function-callers-dialog{position:relative;width:min(760px,100%);max-height:min(760px,88dvh);overflow:auto;padding:22px;background:#171d28;color:#e5edf8;border:1px solid #64748b;border-radius:12px;box-shadow:0 18px 55px #000b}
.function-callers-dialog h2{margin:0 42px 12px 0;font-size:20px}.message-form .function-callers-close{position:absolute;top:10px;right:12px;min-width:34px;padding:2px 9px;border:0;background:transparent;color:#cbd5e1;font-size:26px}
.function-callers-header,.function-caller-code{margin:8px 0;padding:10px;white-space:pre-wrap;overflow-wrap:anywhere;background:#0f172a;color:#bae6fd;border:1px solid #334155;border-radius:7px;font-family:Consolas,"Courier New",monospace}
.function-callers-summary{color:#cbd5e1}.function-callers-list{display:grid;gap:10px}.function-caller{padding:12px;background:#202735;border:1px solid #3b475a;border-radius:8px}.function-caller h3{margin:0 0 5px;font-size:14px}.function-caller-meta{margin:0;color:#aebbd0;font-size:12px}.function-caller-code{margin-bottom:0;color:#e2e8f0}
.message-form{grid-template-rows:repeat(6,auto);grid-template-areas:'job job' 'projects projects' 'tree tree' 'status status' 'meta meta' 'input send'}
@media(max-width:820px){.project-tree{max-height:20dvh}}

/* The project workspace shares one height between the explorer and live output. */
.app{height:100dvh}
.chat-main.workspace-layout{display:grid;grid-template-columns:minmax(230px,32%) minmax(0,1fr) auto;grid-template-rows:auto auto minmax(0,1fr) auto auto auto auto;grid-template-areas:'header header header' 'banner banner banner' 'tree conversation conversation' 'tree projects projects' 'tree status status' 'tree meta meta' 'tree input send';gap:8px 20px;padding:0 20px 18px;overflow:hidden}
.workspace-layout .topbar{grid-area:header;margin:0 -20px}.workspace-layout .site-announcement{grid-area:banner;margin:0 -20px}
.workspace-layout .message-form{display:contents}
.workspace-layout #chat{grid-area:conversation;padding:12px 0}
.chat-main.workspace-layout:has(.job-status:not([hidden])) #chat{display:none}
.workspace-layout .job-status{grid-area:conversation;align-self:stretch;overflow:hidden}
.chat-main.workspace-layout .job-status:not([hidden]) .thinking{height:100%;max-height:none;min-height:0}
.chat-main.workspace-layout .job-status:not([hidden]) .progress-lines{max-height:none;overflow:auto;overscroll-behavior:contain}
.workspace-layout .project-explorer{align-self:stretch;align-content:start;flex-wrap:wrap;min-height:0;display:grid;grid-template-columns:auto minmax(0,1fr);grid-template-rows:auto minmax(0,1fr);gap:10px 8px;padding-top:4px}
.workspace-layout .project-tree{grid-column:1/-1;align-self:stretch;min-height:0;max-height:none;overflow:auto;overscroll-behavior:contain;scrollbar-gutter:stable}
.workspace-layout .project-explorer:has(.project-budget:not([hidden])){grid-template-rows:auto auto minmax(0,1fr)}
.workspace-layout .project-budget{grid-column:1/-1;line-height:1.4}
.workspace-layout .project-attachments{min-height:0;max-height:18dvh;overflow:auto;align-self:end}
.workspace-layout .project-upload-status:empty{display:none}
.workspace-layout .composer-input-row,.workspace-layout .composer-meta{min-width:0}
.workspace-layout .project-tree,.workspace-layout .progress-lines{scrollbar-color:#64748b #171b23;scrollbar-width:thin}
.chat-main.analysis-active .composer-input-row,.chat-main.analysis-active .composer-meta,.chat-main.analysis-active #send,.chat-main.analysis-active .project-upload-status{display:none}
.chat-main.workspace-layout.analysis-active{grid-template-rows:auto auto minmax(0,1fr) auto;grid-template-areas:'header header header' 'banner banner banner' 'tree conversation conversation' 'tree projects projects'}
.chat-main.workspace-layout:not(.has-projects){grid-template-columns:minmax(0,1fr) auto;grid-template-areas:'header header' 'banner banner' 'conversation conversation' 'projects projects'}
.workspace-layout:not(.has-projects) .project-explorer{display:none}
@media(max-width:820px),(orientation:portrait) and (max-width:1100px){
  .chat-main.workspace-layout{grid-template-columns:minmax(0,1fr) auto;grid-template-rows:auto auto minmax(0,.6fr) minmax(0,1fr) auto auto auto auto;grid-template-areas:'header header' 'banner banner' 'tree tree' 'conversation conversation' 'projects projects' 'status status' 'meta meta' 'input send';gap:7px;padding:0 10px 12px}
  .workspace-layout .topbar,.workspace-layout .site-announcement{margin:0 -10px}
  .chat-main.workspace-layout.analysis-active{grid-template-rows:auto auto minmax(0,.55fr) minmax(0,1fr) auto;grid-template-areas:'header header' 'banner banner' 'tree tree' 'conversation conversation' 'projects projects'}
  .chat-main.workspace-layout.analysis-active:not(.has-projects){grid-template-rows:auto auto minmax(0,1fr) auto;grid-template-areas:'header header' 'banner banner' 'conversation conversation' 'projects projects'}
}
'''

TREE_SCRIPT = r'''
function appendFunctionReviewQuality(body,item){
  if(item.output_budget){const budget=document.createElement('p');budget.className='project-report-note';budget.textContent=describeOutputBudget(item.output_budget);body.append(budget)}
  const quality=item.review_quality;if(!quality)return;
  const label=document.createElement('p'),methods={model:'Model review',deterministic:'Static analysis',fallback:'Static fallback',legacy:'Earlier result: quality unverified',unavailable:'No review result'};
  label.textContent=(methods[quality.method]||'Review')+' · '+quality.status;
  if(typeof quality.model_confidence==='number')label.textContent+=' · Model confidence '+(quality.model_confidence*100).toFixed(1)+'% (self-reported)';
  body.append(label);
  for(const note of quality.notes||[]){const detail=document.createElement('p');detail.className='project-report-status';detail.textContent=note;body.append(detail)}
}
function appendProjectReviewQuality(body,quality){
  if(!quality)return;const panel=document.createElement('section');panel.className='report-section review-quality';
  const heading=document.createElement('h3');heading.textContent='Review quality';
  const coverage=document.createElement('p');coverage.textContent='Completed reviews: '+quality.complete_count+' of '+quality.total_count+' ('+(quality.review_coverage*100).toFixed(1)+'%). '+quality.incomplete_count+' need review or have unverified earlier results.';
  const confidence=document.createElement('p');confidence.textContent='Confidence target: 95%. '+quality.model_confidence_at_least_95_count+' functions have a complete model review with a self-reported score at or above the target. Overall calibrated confidence is not available.';
  const explanation=document.createElement('p');explanation.textContent=quality.confidence_explanation;
  panel.append(heading,coverage,confidence,explanation);body.append(panel);
}
let uploadTargetProjectId=null,projectTreeSequence=0,projectTreeChatId=null,projectTreeInitialSelectionPending=true,projectTreeSignature='',projectTreeStructureSignature='',functionCallersReturnFocus=null;
const projectUploadTarget=document.querySelector('#project-upload-target'),projectTree=document.querySelector('#project-tree'),projectTreeMenu=document.querySelector('#project-tree-menu'),functionCallers=document.querySelector('#function-callers'),functionCallersClose=document.querySelector('#function-callers-close'),functionCallersTitle=document.querySelector('#function-callers-title'),functionCallersHeader=document.querySelector('#function-callers-header'),functionCallersSummary=document.querySelector('#function-callers-summary'),functionCallersList=document.querySelector('#function-callers-list');
function syncWorkspaceLayout(){
  const active=activeWatcherForChat(currentChatId),data=active?.data;
  const analysing=Boolean(active)&&!['completed','failed'].includes(data?.status)&&(data?.job_kind==='project_analysis'||data?.job_kind==='verification_retry'||data?.mode==='analyse');
  const main=document.querySelector('.chat-main'),wasAnalysing=main.classList.contains('analysis-active');
  main.classList.toggle('workspace-layout',currentProjects.length>0||analysing);
  main.classList.toggle('has-projects',currentProjects.length>0);
  main.classList.toggle('analysis-active',analysing);
  document.querySelector('.project-target-label').textContent=analysing?'Project':'Upload to';
  if(analysing){attachmentMenu.hidden=true;attachmentButton.setAttribute('aria-expanded','false');if(!wasAnalysing){closeProjectTreeMenu();if(document.activeElement===message||document.activeElement===attachmentButton||document.activeElement===send)visibleJob?.thinking.stopButton.focus()}}
}
function syncProjectExplorer(){
  syncWorkspaceLayout();
  if(projectTreeChatId!==currentChatId){document.querySelector('#project-budget').hidden=true;projectTreeChatId=currentChatId;uploadTargetProjectId=null;projectTreeInitialSelectionPending=true;projectTreeSignature='';closeProjectTreeMenu()}
  const chatWatcher=activeWatcherForChat(currentChatId),watcherData=chatWatcher?.data,activeProjectId=chatWatcher&&!['completed','failed'].includes(watcherData?.status)&&watcherData?.project_id&&currentProjects.some(project=>project.id===watcherData.project_id)?watcherData.project_id:null;
  if(activeProjectId){uploadTargetProjectId=activeProjectId;projectTreeInitialSelectionPending=false}
  if(projectTreeInitialSelectionPending&&currentProjects.length){uploadTargetProjectId=uploadTargetProjectId||currentProjects[0].id;projectTreeInitialSelectionPending=false}
  if(uploadTargetProjectId&&!currentProjects.some(project=>project.id===uploadTargetProjectId))uploadTargetProjectId=currentProjects[0]?.id||null;
  projectUploadTarget.replaceChildren(new Option('New project',''));
  for(const project of currentProjects)projectUploadTarget.add(new Option(project.name,project.id));
  projectUploadTarget.value=uploadTargetProjectId||'';
  projectUploadTarget.disabled=projectUploading||Boolean(activeProjectId);
  projectUploadTarget.title=activeProjectId?'Project selection is locked to the active analysis until it finishes.':'Choose a project to view or receive uploaded files.';
  const selected=currentProjects.find(project=>project.id===uploadTargetProjectId),watcher=selected&&activeWatcherForProject(selected.id);
  const signature=selected?JSON.stringify([currentChatId,selected.id,selected.name,selected.file_count,selected.total_bytes,selected.main_file_path,selected.function_analysis_status,selected.function_analysis_completed_count,selected.function_analysis_failed_count,selected.function_analysis_skipped_count,watcher?[watcher.jobId,watcher.data?.status,watcher.data?.progress_stage,Math.floor(Date.now()/1000)]:null]):'';
  if(uploadTargetProjectId){if(signature!==projectTreeSignature){projectTreeSignature=signature;loadProjectTree(uploadTargetProjectId)}}else{projectTreeSequence++;projectTreeSignature='';projectTree.replaceChildren()}
}
projectUploadTarget.onchange=()=>{if(projectUploadTarget.disabled){syncProjectExplorer();return}document.querySelector('#project-budget').hidden=true;uploadTargetProjectId=projectUploadTarget.value||null;projectTreeInitialSelectionPending=false;closeProjectTreeMenu();syncProjectExplorer()};
function closeProjectTreeMenu(){projectTreeMenu.hidden=true;projectTreeMenu.replaceChildren()}
document.addEventListener('click',event=>{if(!projectTreeMenu.contains(event.target))closeProjectTreeMenu()});
document.addEventListener('keydown',event=>{if(event.key==='Escape'){closeProjectTreeMenu();closeFunctionCallers()}});
window.addEventListener('resize',closeProjectTreeMenu);
function closeFunctionCallers(){if(functionCallers.hidden)return;functionCallers.hidden=true;functionCallersList.replaceChildren();functionCallersReturnFocus?.focus?.();functionCallersReturnFocus=null}
functionCallersClose.onclick=closeFunctionCallers;
functionCallers.onclick=event=>{if(event.target===functionCallers)closeFunctionCallers()};
function tooltipKey(value){return String(value||'').replace(/\s+/g,' ').trim().toLowerCase()}
function functionTooltip(item){
  const sections=[],keys=[];
  function add(value){const text=String(value||'').trim(),key=tooltipKey(text);if(!key||keys.some(existing=>existing===key||existing.includes(key)))return;sections.push(text);keys.push(key)}
  add(item.header);
  let description=item.description_status==='deterministic'?'Static analysis · '+item.description:item.description||'Unknown';
  const state=item.analysis_state||'pending';
  if(state==='analysing')description='Analysing · '+description;
  else if(state==='paused')description='Analysis paused · '+description;
  else if(state==='failed')description='Analysis review failed'+(item.analysis_error?' · '+item.analysis_error:'')+' · '+description;
  else if(state==='skipped')description='Analysis skipped · '+description;
  add(description);
  const returns=Array.isArray(item.return_lines)?item.return_lines:[];
  const returnRows=[];
  for(const value of returns){
    const code=String(value.code||'').trim(),codeKey=tooltipKey(code);
    if(!codeKey||returnRows.some(existing=>tooltipKey(existing.code)===codeKey))continue;
    returnRows.push({code,type:String(value.return_type||'unknown').trim()||'unknown',flowDependent:Boolean(value.flow_dependent)});
  }
  if(returnRows.length){
    const lines=returnRows.map(value=>value.code+' -> '+value.type);
    add((returnRows.length>1&&returnRows.some(value=>value.flowDependent)?'Flow dependent returns:\n':'')+lines.join('\n'));
  }
  return sections.join('\n\n');
}
async function showFunctionCallers(project,item,row){
  functionCallersReturnFocus=row;functionCallers.hidden=false;functionCallersTitle.textContent='Callers of '+item.qualified_name;functionCallersHeader.textContent=item.header||item.qualified_name;functionCallersSummary.textContent='Loading statically resolved callers...';functionCallersList.replaceChildren();functionCallersClose.focus();
  try{
    const response=await fetch('/api/projects/'+encodeURIComponent(project.id)+'/functions/'+encodeURIComponent(item.id)+'/callers',{cache:'no-store'});if(redirectFor(response))return;const data=await readResponse(response);if(!response.ok)throw Error(errorText(data,response));if(functionCallers.hidden)return;
    functionCallersHeader.textContent=data.function?.header||item.header||item.qualified_name;
    const callers=Array.isArray(data.callers)?data.callers:[];functionCallersSummary.textContent=callers.length?callers.length+' statically resolved call'+(callers.length===1?'':'s')+' in this project.':'No statically resolved project callers were found.';
    for(const caller of callers){const card=document.createElement('article'),heading=document.createElement('h3'),meta=document.createElement('p'),code=document.createElement('pre');card.className='function-caller';heading.textContent=caller.caller_name||'Module-level code';meta.className='function-caller-meta';meta.textContent=caller.path+':'+caller.start_line+(caller.usage_kind&&caller.usage_kind!=='unknown'?' · '+caller.usage_kind:'');code.className='function-caller-code';code.textContent=caller.code||caller.callee;card.append(heading,meta,code);functionCallersList.append(card)}
  }catch(error){if(!functionCallers.hidden)functionCallersSummary.textContent='Could not load callers: '+error.message}
}
function showProjectTreeMenu(event,project,entry){
  event.preventDefault();event.stopPropagation();projectTreeMenu.replaceChildren();
  const busy=projectUploading||Boolean(activeWatcherForProject(project.id));
  function action(label,callback){const button=document.createElement('button');button.type='button';button.role='menuitem';button.textContent=label;button.disabled=busy;button.onclick=()=>{closeProjectTreeMenu();callback()};projectTreeMenu.append(button)}
  if(entry.kind==='project'){
    action('Rename',()=>renameAttachedProject(project));
    action('Delete',()=>deleteAttachedProject(project));
  }else if(entry.kind==='function'){
    if(entry.description_status!=='available')action('Get description',()=>requestFunctionDescription(project,entry));
  }else if(entry.kind==='file'&&!entry.is_binary&&entry.analysis_eligible){
    const isMain=project.main_file_path===entry.path;
    action(isMain?'Clear Entry Point':'Set Entry Point',()=>editProjectTree(project,'main-file','PUT',{file_id:isMain?null:entry.file_id}));
  }
  if(!['project','function'].includes(entry.kind))action('Delete',()=>{if(confirm('Delete '+entry.label+(entry.kind==='file'?'':' and its contents')+' from this project?'))editProjectTree(project,'entries','DELETE',{kind:entry.kind,file_id:entry.file_id,batch_id:entry.batch_id,path:entry.path})});
  projectTreeMenu.hidden=false;
  const rect=event.currentTarget?.getBoundingClientRect?.();
  const x=event.clientX||rect?.left||8,y=event.clientY||rect?.bottom||8;
  projectTreeMenu.style.left=Math.max(8,Math.min(x,window.innerWidth-projectTreeMenu.offsetWidth-8))+'px';
  projectTreeMenu.style.top=Math.max(8,Math.min(y,window.innerHeight-projectTreeMenu.offsetHeight-8))+'px';
  projectTreeMenu.querySelector('button')?.focus();
}
function treeEntryLabel(project,entry){
  const row=document.createElement('span');row.className='tree-row';
  const name=document.createElement('span');name.className='tree-name';name.textContent=entry.label;row.append(name);
  if(entry.kind==='file'&&entry.path===project.main_file_path){const badge=document.createElement('span');badge.className='tree-main';badge.textContent='Entry Point';row.append(badge)}
  if(entry.kind==='upload'){const kind=document.createElement('span');kind.className='tree-kind';kind.textContent='('+entry.source_kind.toUpperCase()+')';row.append(kind)}
  const hasActions=entry.kind!=='function'||entry.description_status!=='available';
  if(hasActions){const actions=document.createElement('button');actions.type='button';actions.className='tree-actions';actions.textContent='⋮';actions.setAttribute('aria-label','Actions for '+entry.label);actions.onclick=event=>showProjectTreeMenu(event,project,entry);row.append(actions);row.oncontextmenu=event=>showProjectTreeMenu(event,project,entry);row.onkeydown=event=>{if(event.key==='ContextMenu'||event.key==='F10'&&event.shiftKey)showProjectTreeMenu(event,project,entry)}}
  if(entry.kind==='file'){row.dataset.fileId=String(entry.file_id);row.classList.add('tree-file');updateTreeFileState(row,project,entry)}
  if(entry.kind==='function'){row.dataset.symbolId=String(entry.symbol_id);row.classList.add('tree-function');row.tabIndex=0;row.setAttribute('role','button');row.setAttribute('aria-label','Show callers of '+entry.qualified_name);row.onclick=event=>{if(!event.target.closest('.tree-actions'))showFunctionCallers(project,entry,row)};row.addEventListener('keydown',event=>{if((event.key==='Enter'||event.key===' ')&&!event.target.closest('.tree-actions')){event.preventDefault();showFunctionCallers(project,entry,row)}});updateTreeFunctionState(row,entry)}
  if(entry.kind==='project'){row.dataset.projectId=project.id;row.classList.add('tree-project')}
  return row;
}
function updateTreeFileState(row,project,file){
  const state=file.analysis_state||'pending',total=Number(file.function_count)||0,processed=Number(file.processed_function_count)||0,failed=Number(file.failed_function_count)||0,skipped=Number(file.skipped_function_count)||0,errors=Number(file.error_function_count)||0,warnings=Number(file.warning_function_count)||0,passed=Math.max(0,processed-failed-skipped),hasErrors=errors>0,hasReviewFailures=failed>0,hasWarnings=!hasErrors&&!hasReviewFailures&&(skipped>0||warnings>0);
  row.classList.toggle('is-main',file.path===project.main_file_path);
  row.classList.toggle('is-all-failed',total>0&&failed===total);
  row.classList.toggle('is-mixed',failed>0&&passed>0);
  row.classList.toggle('has-errors',hasErrors);row.classList.toggle('has-review-failures',hasReviewFailures);row.classList.toggle('has-warnings',hasWarnings);
  row.classList.toggle('is-analysing',state==='analysing');row.classList.toggle('is-paused',state==='paused');row.classList.toggle('is-processed',state==='processed');
  row.dataset.analysisState=state;
  let label=total?processed+' of '+total+' functions processed':'No indexed functions';
  if(state==='analysing')label='Analysing functions · '+label;
  else if(state==='paused')label='Analysis paused · '+label;
  if(file.failed_function_count)label+=' · '+file.failed_function_count+' review'+(file.failed_function_count===1?'':'s')+' failed';
  if(file.skipped_function_count)label+=' · '+file.skipped_function_count+' skipped';
  if(errors)label+=' · '+errors+' with errors';
  if(warnings)label+=' · '+warnings+' with warnings';
  if(file.path===project.main_file_path)label='Entry point · '+label;
  if(file.analysis_budget)label+=' · '+describeOutputBudget(file.analysis_budget);
  row.title=label;
  let marker=row.querySelector('.tree-state');
  const mark=state==='processed'?'✓':state==='analysing'?'…':state==='paused'?'Ⅱ':'';
  if(mark){if(!marker){marker=document.createElement('span');marker.className='tree-state';row.insertBefore(marker,row.querySelector('.tree-actions'))}marker.textContent=mark;marker.setAttribute('aria-label',label)}else marker?.remove();
}
function updateTreeFunctionState(row,item){
  const state=item.analysis_state||'pending',errors=Number(item.error_count)||0,warnings=Number(item.warning_count)||0,hasErrors=errors>0,hasWarnings=!hasErrors&&(state==='skipped'||warnings>0);
  for(const name of ['analysing','paused','processed','failed','skipped'])row.classList.toggle('is-'+name,state===name);
  row.classList.toggle('has-errors',hasErrors);row.classList.toggle('has-warnings',hasWarnings);
  row.dataset.analysisState=state;
  const label=functionTooltip(item);
  row.title=label;
  let marker=row.querySelector('.tree-state');
  const mark=state==='processed'?'✓':state==='analysing'?'…':state==='paused'?'Ⅱ':state==='failed'?'?':'';
  if(mark){if(!marker){marker=document.createElement('span');marker.className='tree-state';row.insertBefore(marker,row.querySelector('.tree-actions'))}marker.textContent=mark;marker.setAttribute('aria-label',label)}else marker?.remove();
  if(state==='analysing'||state==='paused'){const group=row.closest('.tree-file-group');if(group)group.open=true}
}
function describeOutputBudget(budget){
  const dependencies=Number.isFinite(Number(budget.dependency_count))?Number(budget.dependency_count):(Number(budget.dependencies?.resolved)||0)+(Number(budget.dependencies?.unresolved)||0);
  return 'Complexity '+budget.complexity_score+'/100 · Dependencies '+dependencies+' · Estimated JSON '+budget.estimated_json_tokens+' tokens · '+(budget.request_kind==='batch'?'Shared batch output ':'Output allowance ')+(budget.active_output_tokens||budget.output_tokens)+' tokens'+(budget.ceiling_exceeded?' · Estimate exceeds ceiling':'')+(budget.uncertainty?.length?' · Uncertain estimate':'');
}
function renderProjectTree(data){
  const panel=document.querySelector('#project-budget'),active=data.files.find(file=>['analysing','paused'].includes(file.analysis_state)&&file.analysis_budget);
  panel.hidden=!active;panel.textContent=active?active.analysis_budget.symbol_name+' · '+describeOutputBudget(active.analysis_budget):'';
  const functions=Array.isArray(data.functions)?data.functions:[],functionsByFile=new Map();
  for(const item of functions){const key=String(item.file_id);if(!functionsByFile.has(key))functionsByFile.set(key,[]);functionsByFile.get(key).push(item)}
  const signature=JSON.stringify([data.project.id,data.project.name,data.project.main_file_path,data.uploads,data.files.map(file=>[file.id,file.path,file.upload_batch_id,file.is_binary,file.analysis_eligible]),functions.map(item=>[item.id,item.file_id,item.qualified_name,item.start_line,item.end_line,item.description])]);
  if(projectTree.firstElementChild&&projectTreeStructureSignature===signature){
    const files=new Map(data.files.map(file=>[String(file.id),file]));
    for(const row of projectTree.querySelectorAll('.tree-file')){const file=files.get(row.dataset.fileId);if(file)updateTreeFileState(row,data.project,file)}
    const bySymbol=new Map(functions.map(item=>[String(item.id),item]));
    for(const row of projectTree.querySelectorAll('.tree-function')){const item=bySymbol.get(row.dataset.symbolId);if(item)updateTreeFunctionState(row,item)}
    return;
  }
  projectTreeStructureSignature=signature;
  const project=data.project,root=document.createElement('details');root.open=true;
  const title=document.createElement('summary');title.append(treeEntryLabel(project,{kind:'project',label:project.name}));root.append(title);
  const uploads=document.createElement('ul');root.append(uploads);
  for(const batch of data.uploads){
    const item=document.createElement('li'),group=document.createElement('details'),heading=document.createElement('summary');group.open=true;
    heading.append(treeEntryLabel(project,{kind:'upload',batch_id:batch.id,label:batch.name,source_kind:batch.source_kind}));group.append(heading);item.append(group);uploads.append(item);
    const contents=document.createElement('ul');group.append(contents);const directories=new Map([['',contents]]);
    const batchFiles=data.files.filter(file=>file.upload_batch_id===batch.id),batchName=String(batch.name||'').toLowerCase();
    const collapseFolderRoot=batch.source_kind==='folder'&&batchFiles.length>0&&batchFiles.every(file=>{const parts=file.path.split('/');return parts.length>1&&parts[0].toLowerCase()===batchName});
    for(const file of batchFiles){
      const storedParts=file.path.split('/'),parts=collapseFolderRoot?storedParts.slice(1):storedParts;
      let parent=contents,prefix=collapseFolderRoot?storedParts[0]:'';
      for(const part of parts.slice(0,-1)){
        prefix=prefix?prefix+'/'+part:part;
        const key=prefix.toLowerCase();
        if(!directories.has(key)){const node=document.createElement('li'),folder=document.createElement('details'),label=document.createElement('summary'),children=document.createElement('ul');folder.open=true;label.append(treeEntryLabel(project,{kind:'folder',batch_id:batch.id,path:prefix,label:part}));folder.append(label,children);node.append(folder);parent.append(node);directories.set(key,children)}
        parent=directories.get(key);
      }
      appendTreeFile(parent,project,file,batch.id,parts.at(-1),functionsByFile);
    }
  }
  // Legacy or externally imported records may not have upload provenance.
  for(const file of data.files.filter(file=>file.upload_batch_id===null))appendTreeFile(uploads,project,file,null,file.path,functionsByFile);
  if(!data.files.length){const empty=document.createElement('p');empty.textContent='This project is empty. Use + to add files, a folder or a ZIP.';root.append(empty)}
  projectTree.replaceChildren(root);
}
function appendTreeFile(parent,project,file,batchId,label,functionsByFile){
  const node=document.createElement('li'),items=functionsByFile.get(String(file.id))||[];
  const entry={...file,kind:'file',file_id:file.id,batch_id:batchId,label};
  if(!items.length){node.append(treeEntryLabel(project,entry));parent.append(node);return}
  const group=document.createElement('details'),heading=document.createElement('summary'),children=document.createElement('ul');group.className='tree-file-group';group.open=items.some(item=>['analysing','paused'].includes(item.analysis_state));
  heading.append(treeEntryLabel(project,entry));group.append(heading,children);node.append(group);parent.append(node);
  for(const item of items){const child=document.createElement('li');child.append(treeEntryLabel(project,{...item,kind:'function',symbol_id:item.id,label:item.qualified_name}));children.append(child)}
}
async function loadProjectTree(projectId){
  const sequence=++projectTreeSequence,chatId=currentChatId;
  try{const response=await fetch('/api/projects/'+encodeURIComponent(projectId)+'/tree',{cache:'no-store'});if(redirectFor(response))return;const data=await readResponse(response);if(!response.ok)throw Error(errorText(data,response));if(sequence===projectTreeSequence&&currentChatId===chatId&&uploadTargetProjectId===projectId)renderProjectTree(data)}
  catch(error){if(sequence===projectTreeSequence){projectTreeSignature='';projectTree.textContent='Could not load project files: '+error.message}}
}
async function requestFunctionDescription(project,item){
  if(projectUploading||activeWatcherForProject(project.id))return;
  const chatId=currentChatId;projectUploading=true;projectUploadTarget.disabled=true;projectUploadStatus.className='project-upload-status';projectUploadStatus.textContent='Queuing description for '+item.qualified_name+'...';syncComposerAvailability();
  try{const response=await fetch('/api/projects/'+encodeURIComponent(project.id)+'/functions/'+encodeURIComponent(item.id)+'/description-jobs',{method:'POST',cache:'no-store'});if(redirectFor(response))return;const data=await readResponse(response);if(!response.ok)throw Error(errorText(data,response));if(currentChatId===chatId){if(data.project)currentProjects=currentProjects.map(value=>value.id===project.id?data.project:value);projectUploadStatus.textContent='Analysing '+item.qualified_name+' for its description...';ensureJobWatcher(data,chatId);renderProjectAttachments()}}
  catch(error){if(currentChatId===chatId){projectUploadStatus.className='project-upload-status error';projectUploadStatus.textContent='Could not get description: '+error.message}}
  finally{projectUploading=false;syncComposerAvailability();syncProjectExplorer()}
}
async function renameAttachedProject(project){
  if(projectUploading||activeWatcherForProject(project.id))return;
  let suggestion=project.name,label='Rename project (up to 200 characters):',name;
  while(true){const entered=window.prompt(label,suggestion);if(entered===null)return;name=entered.replace(/\s+/g,' ').trim();if(name.length&&name.length<=200)break;suggestion=name;label='Please enter a project name between 1 and 200 characters:'}
  if(name===project.name)return;
  const chatId=currentChatId;projectUploading=true;projectUploadTarget.disabled=true;projectUploadStatus.className='project-upload-status';projectUploadStatus.textContent='Renaming '+project.name+'...';syncComposerAvailability();
  try{const response=await fetch('/api/projects/'+encodeURIComponent(project.id),{method:'PATCH',cache:'no-store',headers:{'Content-Type':'application/json'},body:JSON.stringify({name})});if(redirectFor(response))return;const data=await readResponse(response);if(!response.ok)throw Error(errorText(data,response));if(currentChatId===chatId&&data.project){currentProjects=currentProjects.map(item=>item.id===project.id?data.project:item);renderProjectAttachments();projectUploadStatus.textContent='Renamed project to '+data.project.name+'.';if(projectReportState?.projectId===project.id){projectReportState.project=data.project;projectReportTitle.textContent=data.project.name}}}
  catch(error){if(currentChatId===chatId){projectUploadStatus.className='project-upload-status error';projectUploadStatus.textContent='Could not rename project: '+error.message}}
  finally{projectUploading=false;syncComposerAvailability();syncProjectExplorer()}
}
async function editProjectTree(project,endpoint,method,payload){
  if(projectUploading||activeWatcherForProject(project.id))return;
  const chatId=currentChatId;let refresh=false;projectUploading=true;projectUploadTarget.disabled=true;syncComposerAvailability();
  try{const response=await fetch('/api/projects/'+encodeURIComponent(project.id)+'/'+endpoint,{method,cache:'no-store',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});if(redirectFor(response))return;const data=await readResponse(response);if(!response.ok)throw Error(errorText(data,response));if(currentChatId===chatId){currentProjects=currentProjects.map(item=>item.id===project.id?data.project:item);refresh=true;projectUploadStatus.className='project-upload-status';projectUploadStatus.textContent=endpoint==='entries'?'Deleted '+data.deleted_file_count+' file(s). Analysis has been reset.':'Entry point updated. Analysis has been reset.';if(projectReportState?.projectId===project.id)closeProjectReport()}}
  catch(error){if(currentChatId===chatId){projectUploadStatus.className='project-upload-status error';projectUploadStatus.textContent=error.message}}
  finally{projectUploading=false;syncComposerAvailability();if(refresh){projectTreeSignature='';projectTreeStructureSignature='';renderProjectAttachments()}else syncProjectExplorer()}
}
'''

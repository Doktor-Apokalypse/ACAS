// Optional Windows browser regression: node tests/project_tree_browser.cjs
// Uses an isolated Edge profile and a loopback fixture server, never the live DB.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');
const {spawn} = require('node:child_process');

const root = path.resolve(__dirname, '..');
const web = fs.readFileSync(path.join(root, 'web_assets.py'), 'utf8');
const tree = fs.readFileSync(path.join(root, 'project_tree_assets.py'), 'utf8');
const template = name => tree.match(new RegExp(name + " = r'''([\\s\\S]*?)'''"))[1];
const html = web.match(/\nHTML = r"""([\s\S]*?)"""/)[1]
  .replace('</style>', template('TREE_CSS') + '</style>')
  .replace('<div id="project-upload-status"', template('TREE_HTML') + '<div id="project-upload-status"')
  .replace('</script>', template('TREE_SCRIPT') + '\n</script>')
  .replace(/\{\{([A-Z_]+)\}\}/g, (_, key) => ({USERNAME: 'Developer', MAX_MESSAGE_CHARS: '100000', DIRECT_MESSAGE_CHARS: '8000'}[key] || ''));
const project = {id:'qa-project',chat_id:'qa-chat',name:'Multi-language engine',file_count:3,total_bytes:100,primary_language:'python',main_file_path:null,inventory_status:'completed',parser_status:'completed',structure_status:'completed',function_analysis_status:'pending',function_analysis_total_count:3};
let uploads = [{id:1,name:'src',source_kind:'folder'},{id:2,name:'library.zip',source_kind:'zip'}];
let files = [{id:1,path:'src/main.py',upload_batch_id:1},{id:2,path:'src/tools/helper.py',upload_batch_id:1},{id:3,path:'lib/helper.cpp',upload_batch_id:2}].map(file=>({...file,analysis_eligible:1,is_binary:0,function_count:2,processed_function_count:file.id===1?1:0,processing_function_count:0,failed_function_count:0,skipped_function_count:0,error_function_count:0,warning_function_count:0}));
const functions=[
  {id:101,file_id:1,name:'build_verified_source_inventory',qualified_name:'build_verified_source_inventory',start_line:10,end_line:35,header:'def build_verified_source_inventory(source: str) -> list[str]:',return_lines:[{line:35,code:'return inventory',return_type:'list[str]',flow_dependent:false}],description:'Builds a verified inventory of source identifiers.',description_status:'available'},
  {id:102,file_id:1,name:'request_function_analysis',qualified_name:'request_function_analysis',start_line:38,end_line:61,header:'def request_function_analysis(task: Task) -> Result:',return_lines:[],description:null,description_status:'unknown'},
  {id:201,file_id:2,name:'helper_one',qualified_name:'helper_one',start_line:1,end_line:3,header:'def helper_one(value: object) -> object:',return_lines:[{line:2,code:'return value',return_type:'object',flow_dependent:true},{line:3,code:'return None',return_type:'null',flow_dependent:true}],description:'Checks a source-derived condition and returns the validated object.',description_status:'deterministic'},
  {id:202,file_id:2,name:'helper_two',qualified_name:'helper_two',start_line:5,end_line:8,header:'def helper_two():',return_lines:[],description:null,description_status:'unknown'},
  {id:301,file_id:3,name:'parse_value',qualified_name:'parse_value',start_line:2,end_line:9,header:'int parse_value(string value) {',return_lines:[],description:null,description_status:'unknown'},
  {id:302,file_id:3,name:'write_value',qualified_name:'write_value',start_line:11,end_line:18,header:'void write_value(int value) {',return_lines:[],description:null,description_status:'unknown'},
].map(item=>({...item,error_count:0,warning_count:0}));
const mutations = [], errors = [];
let activeJob=null;
files[0].analysis_budget={symbol_name:'build_verified_source_inventory',complexity_score:42,dependency_score:58,dependency_count:5,estimated_json_tokens:1200,output_tokens:8192,uncertainty:[]};
const server = http.createServer(async (req,res) => {
  const chunks=[];for await(const chunk of req)chunks.push(chunk);const body=Buffer.concat(chunks).toString();
  res.setHeader('Content-Type',req.url==='/'?'text/html; charset=utf-8':'application/json');
  let data={};
  if(req.url==='/')return res.end(html);
  if(['/assets/pause.png','/assets/play.png','/assets/stop.png'].includes(req.url)){res.setHeader('Content-Type','image/png');return res.end(fs.readFileSync(path.join(root,req.url.slice(1))))}
  if(req.url.startsWith('/api/chats')&&req.method==='GET')data=req.url==='/api/chats'?{chats:[{id:'qa-chat',title:'Explorer check'},{id:'empty-chat',title:'Other chat'}]}:req.url.endsWith('/empty-chat')?{messages:[],projects:[]}:{messages:[],projects:[project],active_job:activeJob&&['queued','processing'].includes(activeJob.status)?activeJob:null};
  if(req.url.endsWith('/analysis-jobs')){project.function_analysis_status='running';activeJob={job_id:'qa-job',job_kind:'project_analysis',project_id:project.id,mode:'analyse',status:'processing',progress_stage:'analyzing_function',progress_current:45,progress_total:494,progress_file_current:1,progress_file_total:24,progress_file_path:'src/main.py',progress_function_current:45,progress_function_total:65,progress_symbol_name:'build_verified_source_inventory',elapsed_seconds:632,progress_log:Array.from({length:70},(_,index)=>({elapsed:index*9,message:'src/main.py / analyse_function_'+index}))};data=activeJob}
  if(req.url==='/api/chat-jobs/qa-job')data=activeJob;
  if(req.url.endsWith('/qa-job/pause')){activeJob.progress_stage='paused';data={elapsed_seconds:632}}
  if(req.url.endsWith('/qa-job/resume')){activeJob.progress_stage='analyzing_function';data={elapsed_seconds:632}}
  if(req.url.endsWith('/qa-job/cancel')){activeJob.status='failed';activeJob.error='Cancelled by user.';project.function_analysis_status='cancelled'}
  if(req.url.endsWith('/analysis-jobs'))for(const file of files){file.processed_function_count=0;file.processing_function_count=file.id===1?1:0;file.failed_function_count=0}
  if(req.url.endsWith('/tree'))data={project,uploads,files:files.map(file=>({...file,analysis_state:activeJob?.status==='processing'&&file.processing_function_count?(activeJob.progress_stage==='paused'?'paused':'analysing'):file.function_count&&file.processed_function_count===file.function_count?'processed':file.function_count?'pending':'no_functions'})),functions:functions.filter(item=>files.some(file=>file.id===item.file_id)).map(item=>{const file=files.find(value=>value.id===item.file_id),ordinal=functions.filter(value=>value.file_id===item.file_id).findIndex(value=>value.id===item.id),passed=Math.max(0,file.processed_function_count-file.failed_function_count-file.skipped_function_count),analysis_state=activeJob?.status==='processing'&&file.processing_function_count&&ordinal===0?(activeJob.progress_stage==='paused'?'paused':'analysing'):ordinal<passed?'processed':ordinal<passed+file.failed_function_count?'failed':ordinal<file.processed_function_count?'skipped':'pending';return {...item,analysis_state,analysis_error:analysis_state==='failed'?'Incomplete model review: unresolved parameter type':null}})};
  if(req.url==='/api/projects/qa-project/functions/101/callers')data={function:functions[0],caller_count:2,callers:[{id:1,caller_symbol_id:102,caller_name:'request_function_analysis',path:'src/main.py',start_line:54,usage_kind:'assignment',callee:'build_verified_source_inventory',code:'build_verified_source_inventory(source)'},{id:2,caller_symbol_id:null,caller_name:null,path:'src/main.py',start_line:70,usage_kind:'statement',callee:'build_verified_source_inventory',code:'build_verified_source_inventory(default_source)'}]};
  if(req.url.endsWith('/main-file')){const payload=JSON.parse(body);project.main_file_path=files.find(file=>file.id===payload.file_id)?.path||null;mutations.push(payload);data={project}}
  if(req.url.endsWith('/entries')){const payload=JSON.parse(body);const before=files.length;files=files.filter(file=>payload.kind==='file'?file.id!==payload.file_id:!(file.upload_batch_id===payload.batch_id&&(payload.kind==='upload'||file.path.startsWith(payload.path+'/'))));uploads=uploads.filter(batch=>files.some(file=>file.upload_batch_id===batch.id));project.file_count=files.length;if(!files.some(file=>file.path===project.main_file_path))project.main_file_path=null;mutations.push(payload);data={project,deleted_file_count:before-files.length}}
  if(req.url==='/api/projects/qa-project'&&req.method==='PATCH'){const payload=JSON.parse(body);project.name=payload.name;mutations.push({rename:payload.name});data={project}}
  if(req.url==='/api/projects'&&req.method==='POST'){assert.match(body,/name="project_id"\r\n\r\nqa-project/);uploads.push({id:3,name:'extra.py',source_kind:'files'});files.push({id:4,path:'extra.py',upload_batch_id:3,analysis_eligible:1,is_binary:0});project.file_count=files.length;project.total_bytes+=10;mutations.push({upload:true});data=project}
  res.end(JSON.stringify(data));
});
const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
let browser, socket, temporary;
(async()=>{
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  temporary=fs.mkdtempSync(path.join(os.tmpdir(),'apokalypse-tree-browser-'));
  const executable=process.env.EDGE_BINARY||'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';
  browser=spawn(executable,['--headless=new','--disable-gpu','--no-first-run','--no-default-browser-check','--remote-debugging-port=0','--remote-allow-origins=*','--user-data-dir='+temporary,'about:blank'],{windowsHide:true,stdio:'ignore'});
  browser.on('error',error=>errors.push(error.message));
  const activePort=path.join(temporary,'DevToolsActivePort');
  for(let count=0;!fs.existsSync(activePort)&&count<100;count++)await pause(100);
  assert.ok(fs.existsSync(activePort),'Edge debugging endpoint did not start: '+errors.join('; '));
  const port=fs.readFileSync(activePort,'utf8').split('\n')[0];
  const pages=await (await fetch('http://127.0.0.1:'+port+'/json/list',{signal:AbortSignal.timeout(10000)})).json();
  socket=new WebSocket(pages.find(page=>page.type==='page').webSocketDebuggerUrl);
  await new Promise((resolve,reject)=>{const timer=setTimeout(()=>reject(Error('Debug socket timeout')),10000);socket.addEventListener('open',()=>{clearTimeout(timer);resolve()},{once:true});socket.addEventListener('error',()=>{clearTimeout(timer);reject(Error('Debug socket failed'))},{once:true})});
  let nextId=0;const pending=new Map();
  socket.addEventListener('message',event=>{const message=JSON.parse(event.data);if(message.id){const [resolve,reject]=pending.get(message.id)||[];pending.delete(message.id);message.error?reject?.(Error(message.error.message)):resolve?.(message.result)}else if(message.method==='Runtime.exceptionThrown')errors.push(message.params.exceptionDetails.text+': '+message.params.exceptionDetails.exception?.description)});
  const cdp=(method,params={})=>new Promise((resolve,reject)=>{const id=++nextId,timer=setTimeout(()=>{pending.delete(id);reject(Error('Debug command timeout: '+method))},10000);pending.set(id,[value=>{clearTimeout(timer);resolve(value)},error=>{clearTimeout(timer);reject(error)}]);socket.send(JSON.stringify({id,method,params}))});
  const evaluate=async expression=>{const result=await cdp('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true});if(result.exceptionDetails)throw Error(result.exceptionDetails.exception?.description);return result.result.value};
  const until=async expression=>{for(let count=0;count<100;count++){if(await evaluate(expression))return;await pause(100)}throw Error('Timed out: '+expression+'; '+errors.join('; '))};
  await cdp('Runtime.enable');await cdp('Page.enable');
  await cdp('Emulation.setDeviceMetricsOverride',{width:1280,height:950,deviceScaleFactor:1,mobile:false});
  await cdp('Page.navigate',{url:'http://127.0.0.1:'+server.address().port+'/'});
  await until("document.querySelector('#project-tree')?.textContent.includes('main.py')");
  assert.equal(await evaluate('document.title'),'Apokalypse Code Analysis System');
  assert.equal(await evaluate("document.querySelector('.sidebar')===null"),true);
  assert.equal(await evaluate("document.querySelector('#mode-picker')===null&&selectedRequestMode()==='analyse'"),true);
  assert.equal(await evaluate("Math.round(document.querySelector('.app').getBoundingClientRect().width)"),1280);
  assert.equal(await evaluate("projectUploadTarget.value"),'qa-project');
  assert.equal(await evaluate("getComputedStyle(document.querySelector('.tree-function[data-analysis-state=\"pending\"]')).color"),'rgb(174, 184, 199)');
  await evaluate("document.querySelector('[aria-label=\"Actions for Multi-language engine\"]').click()");
  assert.deepEqual(await evaluate("[...projectTreeMenu.querySelectorAll('button')].map(button=>button.textContent)"),['Rename','Delete']);
  await evaluate("window.prompt=()=> 'Renamed analysis project';[...projectTreeMenu.querySelectorAll('button')].find(button=>button.textContent==='Rename').click()");
  await until("document.querySelector('[aria-label=\"Actions for Renamed analysis project\"]')&&!projectUploading");
  assert.equal(await evaluate("projectUploadTarget.selectedOptions[0].textContent"),'Renamed analysis project');
  assert.equal(await evaluate("[...projectTree.querySelectorAll('summary .tree-name')].filter(node=>node.textContent==='src').length"),1,'A folder upload must not repeat its root directory beneath the upload branch');
  assert.match(await evaluate("projectTree.textContent"),/library.zip/);
  assert.match(await evaluate("projectTree.textContent"),/build_verified_source_inventory/);
  assert.match(await evaluate("document.querySelector('.tree-function[data-symbol-id=\"101\"]').title"),/^def build_verified_source_inventory.*Builds a verified inventory.*return inventory -> list\[str\]/s);
  assert.equal(await evaluate("document.querySelector('.tree-function[data-symbol-id=\"102\"]').title"),'def request_function_analysis(task: Task) -> Result:\n\nUnknown');
  assert.match(await evaluate("document.querySelector('.tree-function[data-symbol-id=\"201\"]').title"),/^def helper_one.*Static analysis.*Flow dependent returns:\nreturn value -> object\nreturn None -> null/s);
  await evaluate("document.querySelector('.tree-function[data-symbol-id=\"101\"]').click()");
  await until("!document.querySelector('#function-callers').hidden&&document.querySelectorAll('.function-caller').length===2");
  assert.match(await evaluate("document.querySelector('#function-callers').textContent"),/request_function_analysis/);
  assert.match(await evaluate("document.querySelector('#function-callers').textContent"),/Module-level code/);
  assert.match(await evaluate("document.querySelector('#function-callers').textContent"),/build_verified_source_inventory\(source\)/);
  await evaluate("document.querySelector('#function-callers-close').click()");
  assert.equal(await evaluate("document.querySelector('#function-callers').hidden"),true);
  await evaluate("document.querySelector('[aria-label=\"Actions for helper_one\"]').click()");
  assert.equal(await evaluate("projectTreeMenu.querySelector('button').textContent"),'Get description');
  await evaluate("closeProjectTreeMenu()");
  assert.equal(await evaluate("(()=>{const chip=document.querySelector('.project-chip'),actions=chip.querySelector('.project-chip-actions');return Math.abs(chip.getBoundingClientRect().right-actions.getBoundingClientRect().right)<12})()"),true,'Project action buttons should stay against the right edge');
  await evaluate("document.querySelector('[aria-label=\"Actions for request_function_analysis\"]').click()");
  assert.equal(await evaluate("projectTreeMenu.querySelector('button').textContent"),'Get description');
  await evaluate("closeProjectTreeMenu()");
  await evaluate("document.querySelector('[aria-label=\"Actions for main.py\"]').click(); [...projectTreeMenu.querySelectorAll('button')].find(button=>button.textContent==='Set Entry Point').click()");
  await until("document.querySelector('.tree-main')?.textContent==='Entry Point'");
  assert.equal(await evaluate("getComputedStyle(document.querySelector('.tree-file.is-main .tree-main')).color"),'rgb(74, 222, 128)');
  assert.equal(await evaluate("getComputedStyle(document.querySelector('.tree-file.is-main .tree-name')).color"),'rgb(74, 222, 128)');
  await evaluate("document.querySelector('[aria-label=\"Actions for helper.py\"]').click(); [...projectTreeMenu.querySelectorAll('button')].find(button=>button.textContent==='Set Entry Point').click()");
  await until("document.querySelector('.tree-file[data-file-id=\"2\"].is-main')");
  assert.equal(await evaluate("getComputedStyle(document.querySelector('.tree-file[data-file-id=\"2\"] .tree-name')).color"),'rgb(74, 222, 128)');
  await evaluate("document.querySelector('[aria-label=\"Actions for main.py\"]').click(); [...projectTreeMenu.querySelectorAll('button')].find(button=>button.textContent==='Set Entry Point').click()");
  await until("document.querySelector('.tree-file[data-file-id=\"1\"].is-main')");
  await evaluate("window.confirm=()=>true;document.querySelector('[aria-label=\"Actions for tools\"]').closest('.tree-row').dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,clientX:100,clientY:100}));[...projectTreeMenu.querySelectorAll('button')].find(button=>button.textContent==='Delete').click()");
  await until("!projectTree.textContent.includes('helper.py')&&!projectUploading");
  assert.equal(files.some(file=>file.path==='lib/helper.cpp'),true);
  await evaluate("window.prompt=()=>{throw Error('Existing-project uploads must not ask for a name')}");
  await evaluate("const selected=new DataTransfer();selected.items.add(new File(['def extra(): pass'],'extra.py'));filesInput.files=selected.files;filesInput.dispatchEvent(new Event('change'))");
  await until("projectTree.textContent.includes('extra.py')&&!projectUploading");
  assert.equal(await evaluate("filesInput.value"),'');
  await evaluate("projectUploadTarget.value='';projectUploadTarget.dispatchEvent(new Event('change'))");
  assert.equal(await evaluate('uploadTargetProjectId'),null);
  const mutationsBeforeCancel=mutations.length;
  await evaluate("window.prompt=()=>null;const cancelled=new DataTransfer();cancelled.items.add(new File(['pass'],'cancelled.py'));filesInput.files=cancelled.files;uploadProjectFiles('files',filesInput.files)");
  assert.equal(await evaluate("filesInput.value"),'');
  assert.equal(mutations.length,mutationsBeforeCancel,'Cancel must not send an upload');
  // Capture each new-project multipart submission without changing the fixture project.
  await evaluate("window.originalUploadFetch=window.fetch;window.namedUploads=[];window.fetch=async(url,options)=>{if(url==='/api/projects'&&options?.method==='POST'){namedUploads.push({name:options.body.get('project_name'),target:options.body.get('project_id'),kind:options.body.get('source_kind')});return new Response(JSON.stringify({detail:'Simulated upload failure; selection may be retried'}),{status:422,headers:{'Content-Type':'application/json'}})}return originalUploadFetch(url,options)}");
  for(const kind of ['files','folder','zip']){
    const result=await evaluate(`(async()=>{const file=new File(['pass'],${JSON.stringify(kind==='zip'?'source.zip':'main.py')});Object.defineProperty(file,'webkitRelativePath',{value:'src/main.py'});const defaults=[];const answers=['   ','x'.repeat(201),'  Comparison engine  '];window.prompt=(label,value)=>{defaults.push(value);return answers.shift()};await uploadProjectFiles(${JSON.stringify(kind)},[file]);return {defaults,upload:namedUploads.at(-1)}})()`);
    assert.equal(result.defaults.length,3,'Invalid names must prompt again');
    assert.equal(result.defaults[0],kind==='folder'?'src':kind==='zip'?'source':'main.py');
    assert.deepEqual(result.upload,{name:'Comparison engine',target:null,kind});
    assert.equal(await evaluate('projectUploading'),false);
  }
  await evaluate("window.fetch=window.originalUploadFetch;delete window.originalUploadFetch;window.prompt=()=>{throw Error('Unexpected name popup')}");
  await evaluate("projectUploadTarget.value='qa-project';projectUploadTarget.dispatchEvent(new Event('change'))");
  await until("projectTree.textContent.includes('extra.py')");
  await evaluate("currentProjects.push({...currentProjects[0],id:'comparison-project',name:'Comparison project',function_analysis_status:'completed'});renderProjectAttachments();projectUploadTarget.value='comparison-project';projectUploadTarget.dispatchEvent(new Event('change'))");
  assert.equal(await evaluate("projectUploadTarget.value"),'comparison-project','Completed projects can be selected independently');
  assert.equal(await evaluate("projectUploadTarget.disabled"),false);
  assert.equal(await evaluate("document.querySelector('.project-explorer').getBoundingClientRect().width>300"),true);
  const screenshot=await cdp('Page.captureScreenshot',{format:'png'});
  const screenshotPath=path.join(os.tmpdir(),'apokalypse-project-tree.png');fs.writeFileSync(screenshotPath,Buffer.from(screenshot.data,'base64'));
  await cdp('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});
  assert.equal(await evaluate('document.documentElement.scrollWidth<=window.innerWidth'),true);
  await cdp('Emulation.setDeviceMetricsOverride',{width:1600,height:1000,deviceScaleFactor:1,mobile:false});
  await evaluate("message.value='Keep my draft';document.querySelector('[data-project-id=\"qa-project\"]').click()");
  await until("document.querySelector('.analysis-active')&&visibleJob");
  assert.equal(await evaluate("projectUploadTarget.disabled"),true,'Project selector is locked during project analysis');
  assert.equal(await evaluate("projectUploadTarget.value"),'qa-project','Project selector follows the project being analysed');
  await evaluate("projectUploadTarget.value='comparison-project';projectUploadTarget.dispatchEvent(new Event('change'))");
  assert.equal(await evaluate("projectUploadTarget.value"),'qa-project','A programmatic change cannot switch the tree away from the active analysis');
  await until("document.querySelector('.tree-file.is-main.is-analysing')");
  await until("document.querySelector('.tree-function.is-analysing')");
  assert.equal(await evaluate("document.querySelector('.tree-function.is-analysing').closest('.tree-file-group').open"),true);
  assert.equal(await evaluate("document.querySelector('#project-budget').hidden"),false);
  assert.match(await evaluate("document.querySelector('#project-budget').textContent"),/Complexity 42\/100.*Dependencies 5.*Estimated JSON 1200 tokens.*Output allowance 8192 tokens/);
  assert.match(await evaluate("document.querySelector('.tree-file.is-main').title"),/Complexity 42\/100/);
  assert.equal(await evaluate("getComputedStyle(document.querySelector('.tree-file.is-main .tree-name')).color"),'rgb(34, 211, 238)');
  assert.equal(await evaluate("getComputedStyle(document.querySelector('.tree-function.is-analysing')).color"),'rgb(34, 211, 238)');
  const visible=selector=>evaluate(`Boolean(document.querySelector(${JSON.stringify(selector)})?.getClientRects().length)`);
  for(const selector of ['#message','#attachment-button','#send','.composer-meta'])assert.equal(await visible(selector),false,selector+' should be hidden during analysis');
  assert.equal(await visible('.pause-generation'),true);
  assert.equal(await visible('.stop-generation'),true);
  const layout=await evaluate("(()=>{const tree=projectTree.getBoundingClientRect(),log=jobStatus.getBoundingClientRect();return {sideBySide:tree.right<log.left,logHeight:log.height,treeHeight:tree.height,withinViewport:log.bottom<=innerHeight}})()");
  assert.equal(layout.sideBySide,true);assert.ok(layout.logHeight>600);assert.ok(layout.treeHeight>600);assert.equal(layout.withinViewport,true);
  await evaluate("visibleJob.thinking.pauseButton.click()");
  await until("visibleJob.thinking.pauseButton.classList.contains('is-resume')");
  await until("document.querySelector('.tree-file.is-main.is-paused')");
  assert.equal(await evaluate("document.querySelector('.tree-file.is-main').classList.contains('is-analysing')"),false);
  assert.equal(await visible('#message'),false);
  await evaluate("visibleJob.thinking.pauseButton.click()");
  await until("!visibleJob.thinking.pauseButton.classList.contains('is-resume')");
  await until("activeWatcherForChat(currentChatId)?.data.progress_stage==='analyzing_function'");
  await until("document.querySelector('.tree-file.is-main.is-analysing')");
  await evaluate("window.qaMainRow=document.querySelector('.tree-file.is-main');window.qaFolder=qaMainRow.closest('ul').parentElement;qaFolder.open=false");
  files.find(file=>file.id===1).processing_function_count=0;
  files.find(file=>file.id===1).processed_function_count=2;
  files.find(file=>file.id===3).processing_function_count=1;
  await until("document.querySelector('.tree-file.is-main.is-processed')&&document.querySelector('.tree-file[data-file-id=\"3\"].is-analysing')");
  assert.equal(await evaluate("qaMainRow===document.querySelector('.tree-file.is-main')&&!qaFolder.open"),true,'Progress preserves folder expansion and row identity');
  assert.equal(await evaluate("getComputedStyle(qaMainRow.querySelector('.tree-name')).color"),'rgb(74, 222, 128)');
  assert.equal(await evaluate("qaMainRow.querySelector('.tree-state').textContent"),'✓');
  files.find(file=>file.id===1).warning_function_count=1;
  functions.find(item=>item.id===101).warning_count=1;
  await until("document.querySelector('.tree-file.is-main.has-warnings')&&document.querySelector('.tree-function[data-symbol-id=\"101\"].has-warnings')");
  assert.equal(await evaluate("getComputedStyle(document.querySelector('.tree-function[data-symbol-id=\"101\"]')).color"),'rgb(251, 191, 36)');
  files.find(file=>file.id===1).warning_function_count=0;
  functions.find(item=>item.id===101).warning_count=0;
  files.find(file=>file.id===3).processing_function_count=0;
  files.find(file=>file.id===3).processed_function_count=2;
  files.find(file=>file.id===3).failed_function_count=1;
  await until("document.querySelector('.tree-file[data-file-id=\"3\"].is-processed.is-mixed')");
  assert.equal(await evaluate("getComputedStyle(document.querySelector('.tree-file[data-file-id=\"3\"] .tree-name')).color"),'rgb(251, 146, 60)');
  assert.equal(await evaluate("getComputedStyle(document.querySelector('.tree-function.is-failed')).color"),'rgb(251, 146, 60)');
  assert.equal(await evaluate("document.querySelector('.tree-function.is-failed .tree-state').textContent"),'?');
  assert.match(await evaluate("document.querySelector('.tree-function.is-failed').title"),/Analysis review failed · Incomplete model review/);
  assert.match(await evaluate("document.querySelector('.tree-file[data-file-id=\"3\"]').title"),/1 review failed/);
  files.find(file=>file.id===3).failed_function_count=2;
  await until("document.querySelector('.tree-file[data-file-id=\"3\"].is-all-failed')");
  assert.equal(await evaluate("getComputedStyle(document.querySelector('.tree-file[data-file-id=\"3\"] .tree-name')).color"),'rgb(251, 146, 60)');
  files.find(file=>file.id===3).error_function_count=1;
  functions.find(item=>item.id===301).error_count=1;
  await until("document.querySelector('.tree-file[data-file-id=\"3\"].has-errors')&&document.querySelector('.tree-function[data-symbol-id=\"301\"].has-errors')");
  assert.equal(await evaluate("getComputedStyle(document.querySelector('.tree-file[data-file-id=\"3\"] .tree-name')).color"),'rgb(248, 113, 113)');
  assert.equal(await evaluate("getComputedStyle(document.querySelector('.tree-function[data-symbol-id=\"301\"]')).color"),'rgb(248, 113, 113)');
  assert.equal(await evaluate("document.querySelector('.tree-file[data-file-id=\"4\"] .tree-state')===null"),true,'No-function files do not get completion ticks');
  await evaluate("qaFolder.open=true");
  files.find(file=>file.id===1).processed_function_count=0;
  files.find(file=>file.id===1).processing_function_count=1;
  await until("document.querySelector('.tree-file.is-main.is-analysing')");
  const analysisScreenshot=await cdp('Page.captureScreenshot',{format:'png'});
  const analysisScreenshotPath=path.join(os.tmpdir(),'apokalypse-analysis-workspace.png');fs.writeFileSync(analysisScreenshotPath,Buffer.from(analysisScreenshot.data,'base64'));
  await evaluate("selectChat('empty-chat')");
  assert.equal(await visible('#message'),true);
  assert.equal(await evaluate("message.value"),'Keep my draft');
  await evaluate("selectChat('qa-chat')");
  assert.equal(await visible('#message'),false);
  await cdp('Page.reload');
  await until("document.querySelector('.analysis-active')&&projectTree.textContent.includes('main.py')");
  await until("document.querySelector('.tree-file.is-main.is-analysing')");
  await cdp('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});
  assert.equal(await evaluate("(()=>{const log=jobStatus.getBoundingClientRect(),tree=projectTree.getBoundingClientRect(),stop=visibleJob.thinking.stopButton.getBoundingClientRect();return tree.bottom<=log.top&&log.height>150&&tree.height>60&&stop.bottom<=innerHeight&&document.documentElement.scrollWidth<=innerWidth})()"),true);
  const mobileScreenshot=await cdp('Page.captureScreenshot',{format:'png'});
  fs.writeFileSync(path.join(os.tmpdir(),'apokalypse-analysis-mobile.png'),Buffer.from(mobileScreenshot.data,'base64'));
  await evaluate("visibleJob.thinking.stopButton.click()");
  await until("!document.querySelector('.analysis-active')&&!activeWatcherForChat(currentChatId)");
  await until("!document.querySelector('.tree-file.is-analysing')");
  assert.equal(await evaluate("projectUploadTarget.disabled"),false,'Project selector unlocks when analysis stops');
  assert.equal(await visible('#message'),true);
  assert.equal(await visible('#attachment-button'),true);
  for(const terminal of ['completed','failed']){
    project.function_analysis_status='pending';
    await evaluate("selectChat('qa-chat');");
    await evaluate("document.querySelector('[data-project-id=\"qa-project\"]').click()");
    await until("document.querySelector('.analysis-active')&&activeWatcherForChat(currentChatId)");
    activeJob.status=terminal;activeJob.error=terminal==='failed'?'Fixture analysis failure':null;project.function_analysis_status=terminal;
    await until("!document.querySelector('.analysis-active')&&!activeWatcherForChat(currentChatId)");
    assert.equal(await visible('#message'),true,terminal+' restores the composer');
  }
  await evaluate("selectChat('empty-chat')");
  await evaluate("jobWatchers.set('source-layout',{jobId:'source-layout',chatId:currentChatId,data:{job_kind:'chat',mode:'analyse',status:'queued'}});displayJobState(jobWatchers.get('source-layout'));syncComposerAvailability()");
  assert.equal(await visible('#message'),false,'Queued source analysis hides the composer');
  assert.equal(await visible('.project-explorer'),false,'Source analysis does not reserve an empty tree panel');
  assert.equal(await visible('.stop-generation'),true);
  await evaluate("jobWatchers.get('source-layout').data.mode='chat';syncComposerAvailability()");
  assert.equal(await visible('#message'),true,'Ordinary chat keeps the composer visible');
  await evaluate("removeVisibleJob();jobWatchers.delete('source-layout');syncComposerAvailability()");
  await evaluate("const qualityHost=document.createElement('div');qualityHost.id='quality-check';document.body.append(qualityHost);appendProjectReviewQuality(qualityHost,{complete_count:1,total_count:2,incomplete_count:1,review_coverage:.5,model_confidence_at_least_95_count:0,confidence_explanation:'Coverage is not accuracy.'});appendFunctionReviewQuality(qualityHost,{review_quality:{method:'fallback',status:'failed',notes:['<script>invalid proof</script>'],model_confidence:null}})");
  assert.match(await evaluate("document.querySelector('#quality-check').textContent"),/95%/);
  assert.match(await evaluate("document.querySelector('#quality-check').textContent"),/Static fallback/);
  assert.equal(await evaluate("document.querySelector('#quality-check script')===null"),true);
  await evaluate("document.querySelector('#quality-check').remove()");
  assert.deepEqual(errors,[]);
  console.log('Browser passed: active, mixed and failed tree colours, completion ticks, preserved folders, project tree edits, uploads, split layout, hidden analysis composer, pause/resume, chat switching, reload, cancellation, completion, failure and mobile layout.');
  console.log('Screenshot: '+screenshotPath);
  console.log('Analysis screenshot: '+analysisScreenshotPath);
  await cdp('Browser.close').catch(()=>{});
})().catch(error=>{console.error(error);process.exitCode=1}).finally(async()=>{
  socket?.close();browser?.kill();server.close();
  if(temporary){await pause(500);const target=path.resolve(temporary),allowed=path.resolve(os.tmpdir())+path.sep;if(target.startsWith(allowed)&&path.basename(target).startsWith('apokalypse-tree-browser-'))try{fs.rmSync(target,{recursive:true,force:true,maxRetries:5,retryDelay:200})}catch{console.log('Temporary browser profile remains at '+target)}}
});

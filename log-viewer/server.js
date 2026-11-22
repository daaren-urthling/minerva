import http from 'node:http';
import { readFile, stat, readdir } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const publicDir = path.join(__dirname, 'public');
const port = Number(process.env.PORT || 8787);
const defaultSessionsDir = path.resolve(__dirname, '..', '.minerva', 'sessions');
let sessionsDir = process.argv[2] ? path.resolve(process.argv[2]) : (process.env.MINERVA_SESSIONS_DIR ? path.resolve(process.env.MINERVA_SESSIONS_DIR) : defaultSessionsDir);
let selectedLog = null;

const types={'.html':'text/html; charset=utf-8','.js':'text/javascript; charset=utf-8','.css':'text/css; charset=utf-8'};
const send=(res,status,body,type='text/plain; charset=utf-8')=>{res.writeHead(status,{'Content-Type':type,'Cache-Control':'no-store'});res.end(body)};
const json=(res,status,obj)=>send(res,status,JSON.stringify(obj),'application/json; charset=utf-8');
async function body(req){let s='';for await(const c of req){s+=c;if(s.length>1024*1024)throw new Error('Request body too large')}return s}
async function listSessions(){
  const entries=await readdir(sessionsDir,{withFileTypes:true});
  const rows=await Promise.all(entries.filter(e=>e.isFile()&&e.name.toLowerCase().endsWith('.json')).map(async e=>{const full=path.join(sessionsDir,e.name);const i=await stat(full);return{name:e.name,modified_at:i.mtime.toISOString(),size:i.size}}));
  return rows.sort((a,b)=>new Date(b.modified_at)-new Date(a.modified_at));
}

const server=http.createServer(async(req,res)=>{try{
  const url=new URL(req.url,`http://${req.headers.host}`);
  if(url.pathname==='/api/status'&&req.method==='GET') return json(res,200,{sessions_dir:sessionsDir,selected_log:selectedLog});
  if(url.pathname==='/api/directory'&&req.method==='POST'){
    const p=JSON.parse(await body(req)||'{}').path;
    if(!p||typeof p!=='string')return json(res,400,{error:'A directory path is required.'});
    const candidate=path.resolve(p);try{const i=await stat(candidate);if(!i.isDirectory())return json(res,400,{error:'The selected path is not a directory.'})}catch(e){return json(res,404,{error:'Directory not found.',detail:e.message})}
    sessionsDir=candidate;selectedLog=null;return json(res,200,{sessions_dir:sessionsDir});
  }
  if(url.pathname==='/api/sessions'&&req.method==='GET'){
    try{return json(res,200,{sessions_dir:sessionsDir,sessions:await listSessions(),selected_log:selectedLog})}catch(e){return json(res,200,{sessions_dir:sessionsDir,sessions:[],selected_log:selectedLog,warning:e.message})}
  }
  if(url.pathname==='/api/session/select'&&req.method==='POST'){
    const name=JSON.parse(await body(req)||'{}').name;
    if(!name||path.basename(name)!==name||!name.toLowerCase().endsWith('.json'))return json(res,400,{error:'Invalid session file name.'});
    const full=path.resolve(sessionsDir,name);if(path.dirname(full)!==path.resolve(sessionsDir))return json(res,403,{error:'Invalid path.'});
    try{const i=await stat(full);if(!i.isFile())return json(res,400,{error:'Session is not a file.'})}catch(e){return json(res,404,{error:'Session not found.',detail:e.message})}
    selectedLog=name;return json(res,200,{selected_log:selectedLog});
  }
  if(url.pathname==='/api/log'&&req.method==='GET'){
    if(!selectedLog)return json(res,200,{data:null,file:null,message:'No session selected.'});
    const full=path.join(sessionsDir,selectedLog);const [text,i]=await Promise.all([readFile(full,'utf8'),stat(full)]);
    try{return json(res,200,{data:JSON.parse(text),file:{name:selectedLog,path:full,modified_at:i.mtime.toISOString(),size:i.size}})}catch(e){return json(res,503,{error:'Log is temporarily not valid JSON, possibly while Minerva is writing it.',detail:e.message})}
  }
  const rel=url.pathname==='/'?'index.html':url.pathname.replace(/^\/+/, '');const target=path.resolve(publicDir,rel);if(!target.startsWith(publicDir+path.sep)&&target!==path.join(publicDir,'index.html'))return send(res,403,'Forbidden');send(res,200,await readFile(target),types[path.extname(target)]||'application/octet-stream');
}catch(e){json(res,e.code==='ENOENT'?404:500,{error:e.message})}});
server.listen(port,'127.0.0.1',()=>{console.log(`Minerva Log Viewer: http://localhost:${port}`);console.log(`Sessions directory: ${sessionsDir}`)});

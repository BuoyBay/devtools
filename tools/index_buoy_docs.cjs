#!/usr/bin/env node
// Index the installed documentation. No network access, including for links/assets.
const fs = require('node:fs');
const path = require('node:path');
const {pathToFileURL} = require('node:url');
const MarkdownIt = require('markdown-it');
const {parseHTML} = require('linkedom');
const {frontMatter,relatedFromHTML}=require('./documentation_metadata.cjs');
const root = path.resolve(__dirname, '..');
const docs = path.resolve(process.argv[2] || '/Applications/Buoy/docs');
const output = path.join(root, 'apps/BuoyDocs/snapshot/docs.json');
const previous = fs.existsSync(output) ? JSON.parse(fs.readFileSync(output)) : {pages:[]};
const md = new MarkdownIt({html:false, linkify:false});
const escape = text => md.utils.escapeHtml(text);
const slug = text => text.toLowerCase().replace(/[^\p{L}\p{N}\s_-]/gu,'').trim().replace(/\s+/g,'-');
// Some installed Markdown links use punctuation removal after whitespace substitution.
const legacySlug = text => text.toLowerCase().trim().replace(/\s/g,'-').replace(/[^\p{L}\p{N}_-]/gu,'');
const fragmentTargets = new Map();
const normalize = text => text.toLowerCase().replace(/[^a-z0-9]/g,'');
function safeFile(file) {
  const relative = path.relative(docs, file);
  if (relative.startsWith('..') || path.isAbsolute(relative)) return false;
  let current = docs;
  for (const component of relative.split(path.sep)) {
    current = path.join(current, component);
    if (fs.lstatSync(current).isSymbolicLink()) return false;
  }
  return fs.lstatSync(file).isFile();
}
function walk(dir) {return fs.readdirSync(dir,{withFileTypes:true}).flatMap(e=>e.isDirectory()?walk(path.join(dir,e.name)):e.isFile()?[path.join(dir,e.name)]:[]);}
const files = walk(docs).filter(f=>/\.(md|html|bui)$/.test(f)).sort();
const filePaths = new Map(files.map(f=>[f,'/local/'+path.relative(docs,f).split(path.sep).map(encodeURIComponent).join('/')]));
const names = new Map();
for(const f of files){const name=path.basename(f).toLowerCase();if(!names.has(name))names.set(name,[]);names.get(name).push(f);}
// Installed docs contain legacy relative paths. Only use unambiguous fallbacks.
const redirects = {'getting-started.md':'tutorials/getting-started.md','buoy-vs-xojo.md':'documentation/coming-from-xojo.md','optional-and-result.md':'documentation/optional-result.md'};
const missing = new Set();
function resolveFile(value,from){
  let decoded;try{decoded=decodeURIComponent(value);}catch{decoded=value;}
  const relative = decoded.replace(/^\.\//,'');
  const candidates=[path.resolve(path.dirname(from),relative),path.resolve(docs,relative.replace(/^\/?docs\//,'')),path.resolve(docs,'..',relative)];
  for(const c of candidates)if(filePaths.has(c))return c;
  const basename=path.basename(relative).toLowerCase();
  const redirect=redirects[basename];if(redirect&&filePaths.has(path.join(docs,redirect)))return path.join(docs,redirect);
  const matches=names.get(basename)||[];
  return matches.length===1?matches[0]:null;
}
const counts={reference:0,tutorials:0,course:0,examples:0};
const pages=files.map(file=>{
  const relative=path.relative(docs,file).split(path.sep).join('/');
  const input=fs.readFileSync(file,'utf8');
  const parsedMetadata=file.endsWith('.md')?frontMatter(input):{body:input,metadata:{}};
  const source=parsedMetadata.body;
  const example=file.endsWith('.bui');
  const {document}=parseHTML('<html><body>'+(example?'<pre><code>'+escape(source)+'</code></pre>':file.endsWith('.md')?md.render(source):source)+'</body></html>');
  const first=document.querySelector('h1');
  const title=example?path.basename(file)+' · '+path.basename(path.dirname(file)):(first?.textContent||document.querySelector('title')?.textContent||path.basename(file)).trim();
  first?.remove();
  for(const el of document.querySelectorAll('script,style,link,iframe,object,embed,form,input,button,title,meta'))el.remove();
  for(const el of document.querySelectorAll('*'))for(const attr of [...el.attributes])if(attr.name.startsWith('on')||['style','srcset'].includes(attr.name))el.removeAttribute(attr.name);
  const headingElements=[...document.querySelectorAll('h2,h3,h4,h5,h6')];
  const headings=new Set(headingElements);
  const usedIds=new Set([...document.querySelectorAll('[id]')].filter(el=>!headings.has(el)).map(el=>el.id));
  for(const el of headingElements){
    const base=slug(el.textContent);
    let id=base, n=0;
    while(usedIds.has(id))id=base+'-'+(++n);
    usedIds.add(id);
    el.id=id;
  }
  const ids = new Map(), legacy = new Map();
  for(const el of document.querySelectorAll('[id]'))ids.set(el.id,(ids.get(el.id)||0)+1);
  for(const el of document.querySelectorAll('h2,h3,h4,h5,h6')){
    const key=legacySlug(el.textContent);
    if(!legacy.has(key))legacy.set(key,[]);
    legacy.get(key).push(el.id);
  }
  fragmentTargets.set(filePaths.get(file),{ids,legacy});
  for(const a of document.querySelectorAll('a[href]')){
    const href=a.getAttribute('href');
    if(href.startsWith('#'))continue;
    if(/^(https?:|mailto:)/i.test(href))continue;
    if(/^[a-z][a-z0-9+.-]*:/i.test(href)){a.removeAttribute('href');continue;}
    const [filename,hash]=href.split('#');
    const target=resolveFile(filename,file);
    if(target)a.setAttribute('href',filePaths.get(target)+(hash?'#'+hash:''));
    else {const key='/missing/'+encodeURIComponent(relative+' → '+href);missing.add(key);a.setAttribute('href',key);a.setAttribute('title','Not included in the installed documentation');}
  }
  for(const img of document.querySelectorAll('img')){
    const src=img.getAttribute('src')||'';
    const local=path.resolve(path.dirname(file),src);
    if(!local.startsWith(docs+path.sep)||!fs.existsSync(local)||!safeFile(local)){img.replaceWith(document.createTextNode('[Image not included in installed docs: '+(img.getAttribute('alt')||src)+']'));continue;}
    const mime={'.png':'image/png','.jpg':'image/jpeg','.jpeg':'image/jpeg','.gif':'image/gif','.svg':'image/svg+xml','.webp':'image/webp'}[path.extname(local)];
    if(!mime)throw Error('Unsupported local image '+local);
    img.setAttribute('src','data:'+mime+';base64,'+fs.readFileSync(local).toString('base64'));
  }
  const section=example?'examples':relative.startsWith('learning_buoy/')?'course':relative.startsWith('tutorials/')?'tutorials':'reference';
  counts[section]++;
  const metadata=parsedMetadata.metadata;
  const related=[];
  for(const href of metadata.related||[]){const [filename,hash]=href.split('#');const target=filename?resolveFile(filename,file):file;if(target)related.push(filePaths.get(target)+(hash?'#'+hash:''));else throw Error('Missing related topic: '+href+' in '+relative);}
  for(const href of relatedFromHTML(document.body.innerHTML))if(!related.includes(href))related.push(href);
  return {path:filePaths.get(file),title:metadata.title||title,section,relative,source:pathToFileURL(file).href,html:document.body.innerHTML,text:document.body.textContent.replace(/\s+/g,' ').trim(),...metadata,related};
});
// Resolve only known, unique legacy heading fragments after all target pages exist.
// Preserve canonical IDs, exact case, and any existing exact fragment match.
for(const page of pages){
  const repair = href => {
    const split=href.indexOf('#');
    if(split<0)return href;
    const target=fragmentTargets.get(split===0?page.path:href.slice(0,split));
    if(!target)return href;
    let fragment;try{fragment=decodeURIComponent(href.slice(split+1));}catch{return href;}
    if(target.ids.has(fragment))return href;
    const candidates=target.legacy.get(fragment)||[];
    if(candidates.length!==1||target.ids.get(candidates[0])!==1)return href;
    return href.slice(0,split+1)+encodeURIComponent(candidates[0]);
  };
  const {document}=parseHTML('<html><body>'+page.html+'</body></html>');
  let changed=false;
  for(const a of document.querySelectorAll('a[href]')){
    const before=a.getAttribute('href'),after=repair(before);
    if(after!==before){a.setAttribute('href',after);changed=true;}
  }
  if(changed)page.html=document.body.innerHTML;
  page.related=[...new Set(page.related.map(repair))];
}
const bySlug=new Map(),byTitle=new Map();
for(const p of pages.filter(p=>p.section!=='examples')){bySlug.set(path.basename(p.relative).replace(/\.[^.]+$/,''),p.path);byTitle.set(normalize(p.title),p.path);}
const aliases={};
for(const p of previous.pages){const target=byTitle.get(normalize(p.title))||bySlug.get(p.path.split('/').filter(Boolean).pop());if(target)aliases[p.path]=target;}
for(const [old,target] of Object.entries(previous.aliases||{}))if(pages.some(p=>p.path===target))aliases[old]=target;
const index=Object.create(null);
for(let i=0;i<pages.length;i++)for(const token of new Set((pages[i].title+' '+pages[i].text).toLowerCase().match(/[\p{L}\p{N}_]+/gu)||[])){if(!index[token])index[token]=[];index[token].push(i);}
const snapshot={source:pathToFileURL(docs+'/').href,sourceKind:'local',captured:new Date().toISOString(),startPath:filePaths.get(path.join(docs,'tutorials/getting-started.md')),counts,aliases,missing:[...missing].sort(),index,pages};
fs.writeFileSync(output+'.tmp',JSON.stringify(snapshot));fs.renameSync(output+'.tmp',output);
require('node:child_process').execFileSync('python3',[path.join(__dirname,'index_documentation_definitions.py'),output]);
require('node:child_process').execFileSync('python3',[path.join(__dirname,'audit_documentation_keywords.py')]);
console.log(`Indexed ${pages.length} pages: ${JSON.stringify(counts)}, ${Object.keys(index).length} search terms, ${missing.size} unresolved installed-doc links`);

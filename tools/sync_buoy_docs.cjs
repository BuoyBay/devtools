#!/usr/bin/env node
// Explicit online refresh only. Building and running the app never fetch docs.
const fs = require('node:fs');
const path = require('node:path');
const {execFile} = require('node:child_process');
const {promisify} = require('node:util');
const {parseHTML} = require('linkedom');
const {relatedFromHTML}=require('./documentation_metadata.cjs');
const run = promisify(execFile);
const origin = 'https://www.stretchedout.com';
const start = '/buoy/introduction/getting-started/';
const root = path.resolve(__dirname, '..');
const allowed = new Set('p div span h1 h2 h3 h4 h5 h6 pre code a ul ol li dl dt dd table thead tbody tr th td blockquote strong em b i hr br details summary kbd samp figure figcaption img sup sub'.split(' '));
const queue = [start], seen = new Set(queue), pages = [], missing = [], assets = new Map();
async function download(url) {
  const {stdout} = await run('curl', ['--fail', '--silent', '--show-error', '--proto', '=https', '--max-time', '40', '--retry', '2', '--write-out', '\n%{http_code}', url], {maxBuffer: 12 * 1024 * 1024, encoding: 'buffer'});
  const split = stdout.lastIndexOf(10);
  const status = Number(stdout.subarray(split + 1).toString());
  if (status < 200 || status >= 300) throw Error('Unexpected HTTP status: ' + status);
  return stdout.subarray(0, split);
}
function enqueue(href, base) {
  const u = new URL(href, base);
  if (u.origin !== origin || !u.pathname.startsWith('/buoy/') || !u.pathname.endsWith('/')) return;
  if (!seen.has(u.pathname)) { seen.add(u.pathname); queue.push(u.pathname); }
}
async function page(urlPath) {
  const url = origin + urlPath;
  let raw;
  try { raw = await download(url); } catch (e) {
    if (String(e.stderr).includes('404')) { missing.push(urlPath); console.log('Website page missing: '+urlPath); return; }
    throw e;
  }
  const {document} = parseHTML(raw.toString('utf8'));
  if (urlPath === start) for (const a of document.querySelectorAll('a[href]')) enqueue(a.getAttribute('href'), url);
  const content = document.querySelector('.sl-markdown-content');
  if (!content) throw Error('Missing documentation content: ' + url);
  const title = document.querySelector('h1')?.textContent.trim() || urlPath;
  for (const a of content.querySelectorAll('a[href]')) enqueue(a.getAttribute('href'), url);
  for (const el of [...content.querySelectorAll('*')]) {
    const tag = el.tagName.toLowerCase();
    if (['script','style','link','iframe','object','embed','form','button','svg','input'].includes(tag) || el.classList.contains('sl-anchor-link') || el.classList.contains('sr-only')) {el.remove(); continue;}
    if (!allowed.has(tag)) {el.replaceWith(...el.childNodes); continue;}
    const attrs = {};
    for (const name of ['id','href','src','alt','colspan','rowspan']) if (el.hasAttribute(name)) attrs[name] = el.getAttribute(name);
    for (const a of [...el.attributes]) el.removeAttribute(a.name);
    for (const [k,v] of Object.entries(attrs)) el.setAttribute(k,v);
    if (tag === 'a' && attrs.href) {
      const u = new URL(attrs.href, url);
      if (!['https:', 'http:', 'mailto:'].includes(u.protocol)) { el.removeAttribute('href'); continue; }
      el.setAttribute('href', u.origin === origin && u.pathname.startsWith('/buoy/') ? u.pathname + u.hash : u.href);
    }
    if (tag === 'img' && attrs.src) {
      const u = new URL(attrs.src, url);
      if (u.origin !== origin) throw Error('Unbundled external image: '+u);
      if (!assets.has(u.href)) assets.set(u.href, download(u.href).then(bytes => {
        const ext = u.pathname.split('.').pop().toLowerCase();
        const mime = {png:'image/png',jpg:'image/jpeg',jpeg:'image/jpeg',svg:'image/svg+xml',webp:'image/webp',gif:'image/gif'}[ext];
        if (!mime) throw Error('Unknown image type: '+u);
        return 'data:'+mime+';base64,'+bytes.toString('base64');
      }));
      el.setAttribute('src', await assets.get(u.href));
    }
  }
  const section = urlPath.split('/')[2] || 'overview';
  pages.push({relative:urlPath, source:url, path:urlPath, title, section:section.replaceAll('-',' '), html:content.innerHTML, text:content.textContent.replace(/\s+/g,' ').trim()});
}
(async () => {
  // Process batches to keep the request rate bounded while discovering links.
  let offset = 0;
  while (offset < queue.length) {
    if (queue.length > 1200) throw Error('Unexpected crawl size');
    const batch = queue.slice(offset, offset + 4); offset += batch.length;
    await Promise.all(batch.map(page));
    if (offset % 40 === 0) console.log(`Downloaded ${offset}/${queue.length} pages`);
  }
  // Some upstream pages still link to Markdown filenames. Resolve those by title.
  const normalize = text => text.toLowerCase().replace(/[^a-z0-9]/g, '');
  const byTitle = new Map(pages.map(p => [normalize(p.title), p.path]));
  for (const p of pages) {
    const {document} = parseHTML('<html><body>'+p.html+'</body></html>');
    for (const a of document.querySelectorAll('a[href]')) {
      const href = a.getAttribute('href'), u = new URL(href, origin);
      if (u.origin === origin && u.pathname.endsWith('.md')) {
        const title = decodeURIComponent(u.pathname.split('/').pop()).replace(/\.md$/, '');
        const target = byTitle.get(normalize(title));
        if (target) a.setAttribute('href', target + u.hash);
      }
    }
    p.html = document.body.innerHTML;
    p.related = relatedFromHTML(p.html);
  }
  pages.sort((a,b) => a.path.localeCompare(b.path));
  const snapshot = {source:origin+start, captured:new Date().toISOString(), missing, pages};
  const dest = path.join(root,'apps/BuoyDocs/snapshot/docs.json');
  fs.writeFileSync(dest+'.tmp',JSON.stringify(snapshot)); fs.renameSync(dest+'.tmp',dest);
  console.log(`Saved ${pages.length} pages and ${assets.size} embedded images to ${dest}`);
})().catch(e=>{console.error(e);process.exitCode=1;});

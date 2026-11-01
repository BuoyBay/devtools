// Shared metadata handling for installed and explicitly refreshed upstream docs.
const {parseHTML} = require('linkedom');
function frontMatter(source) {
  const opening=/^---\r?\n/.exec(source);
  if (!opening) return {body:source, metadata:{}};
  const closing=/^---(?:\r?\n|$)/m.exec(source.slice(opening[0].length));
  if(!closing) throw Error('Unterminated documentation metadata');
  const end=opening[0].length+closing.index;
  const metadata={};
  for(const line of source.slice(opening[0].length,end).split(/\r?\n/)) {
    if(!line.trim())continue;
    const match=/^(title|summary|category|keywords|symbols|types|related|dependencies|order):\s*(.*)$/.exec(line);
    if(!match)throw Error('Unsupported upstream metadata: '+line);
    const [,key,raw]=match;
    if(Object.hasOwn(metadata,key))throw Error('Duplicate metadata: '+key);
    if(key==='order'&&!/^-?[0-9]{1,6}$/.test(raw))throw Error('Invalid order metadata');
    metadata[key]=key==='order'?Number(raw):raw.startsWith('[')||raw.startsWith('"')?JSON.parse(raw):raw;
    if(['keywords','symbols','types','related','dependencies'].includes(key)&&(!Array.isArray(metadata[key])||metadata[key].some(v=>typeof v!=='string')))throw Error('Invalid '+key+' metadata');
  }
  return {body:source.slice(end+closing[0].length),metadata};
}
function relatedFromHTML(html) {
  const {document}=parseHTML('<html><body>'+html+'</body></html>');
  const result=[];
  function collect(node){for(const a of node.querySelectorAll('a[href]')){const href=a.getAttribute('href');if((href.startsWith('/')&&!href.startsWith('//')||href.startsWith('#'))&&!href.startsWith('/missing/')&&!result.includes(href))result.push(href);}}
  for(const heading of document.querySelectorAll('h2,h3,h4,h5,h6')) {
    if(!/^(see also|related(?: topics| pages| references)?)\s*:?$/i.test(heading.textContent.trim()))continue;
    let node=heading.nextElementSibling;
    while(node&&!/^H[1-6]$/.test(node.tagName)){collect(node);node=node.nextElementSibling;}
  }
  for(const p of document.querySelectorAll('p'))if(/^(see also|related topics)\s*:/i.test(p.textContent.trim()))collect(p);
  return result.slice(0,64);
}
module.exports={frontMatter,relatedFromHTML};

const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const source=fs.readFileSync(path.resolve(__dirname,'../../../../_server_deploy/static/pdf/reader.src/26-figures.js'),'utf8');
const scope=vm.createContext({window:{__figBookOn:true,__figAttached:[]},FILE_REL:'localbook:test',_cache:{}});
vm.runInContext(source.slice(source.indexOf('  function _figId('),source.indexOf('  function _attachFig('))+
  source.slice(source.indexOf('  function _figInk('),source.indexOf('  window.__figInk ='))+
  source.slice(source.indexOf('  window.__bwReaderPageFigures ='),source.indexOf('  // 带入助手（原生面板')),scope);
(async()=>{
  const inputs=[[],[{bbox:[0,0,1,1],caption:'図',desc:'body',group:true}],
    [{bbox:[.1,.2,.8,.9],fbox:[.125,.0625,.625,.8125],badge:[.3,.4]}],
    [{bbox:[1,0,0,1]},{bbox:[0,0,1]},{fbox:[0,0,.5,.5],desc:'ab'.repeat(4000)}]];
  for(let n=1;n<60;n++)inputs.push([{bbox:[n/97,n/113,.8,.9],caption:'图'+n,badge:[.8,.2],group:n%2===0}]);
  const cases=[];
  for(const figures of inputs){scope._cache[3]={figs:figures,pending:false};const rows=await scope.window.__bwReaderPageFigures(3);for(const r of rows)delete r.attached;cases.push({figures,rows});}
  const values=[0,-0,.0625,-.0625,.8125,.9995,.0005,16,-16,Number.MIN_VALUE,...Array.from({length:2000},(_,i)=>(i-1000)/10000)];
  const strokes=[{t:'pen',w:2,c:'#ff0000',p:[[.1,.2],[.0625,.8125]]},{t:'region',w:1,p:[[.95,.96]]}];
  scope.window._ink={byPage:{3:strokes}};
  fs.writeFileSync(process.argv[2],JSON.stringify({cases,rounding:values.map(value=>[value,value.toFixed(3)]),strokes,ink:scope._figInk(3,[0,0,.9,.9])}));
  console.log('Native figure oracle:',cases.length,'pages and',values.length,'coordinates');
})();

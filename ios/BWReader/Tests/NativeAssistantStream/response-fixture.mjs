import {readFileSync, writeFileSync} from 'node:fs';
import vm from 'node:vm';
const source=readFileSync(new URL('../../../../_server_deploy/static/pdf/rc-assistant.js',import.meta.url),'utf8');
const mood=source.slice(source.indexOf('  function stripMoodTag('), source.indexOf('  function splitFollowups('));
const split=source.slice(source.indexOf('  function splitFollowups('),source.indexOf('  function renderFollowups('));
const torn=source.slice(source.indexOf('  function _stripTornFU('),source.indexOf('  function _splitFollowupsNative('));
const context=vm.createContext({}); vm.runInContext(mood+split+torn,context);
const answers=[
  '', '日本語と😀、e\u0301', '[语气:温柔] 今天来复习吧。', '前段【语气:认真】后段',
  '正文[[FOLLOWUP]]1. 为什么？|2. 怎么做？\n• 下一步[[/FOLLOWUP]]',
  '正文[[FOLLOWUP]]未闭合的问题|另一个',
  '前[[FOLLOWUP]]一[[/FOLLOWUP]]中[[FOLLOWUP]]二|三|四|五[[/FOLLOWUP]]后',
  '[语气:开心]公式 $x$ [[FOLLOWUP]] 求解 $x$ [[/FOLLOWUP]]',
  'table\n| A | B |\n|---|---|\n|1|2|', '正文[语气:微笑', '正常的 [方括号] 和 【标题】',
];
for(let length=2;length<'[[FOLLOWUP]]'.length;length++) answers.push('正文 '+'[[FOLLOWUP]]'.slice(0,length));
const cases=answers.map(answer=>{
  const result=context.splitFollowups(answer), voiceText=context._stripTornFU(result.text);
  return {answer,voiceText,displayText:context.stripMoodTag(voiceText).text,
    finalDisplayText:context.stripMoodTag(result.text).text,followups:result.followups};
});
writeFileSync(process.argv[2],JSON.stringify(cases));

// Optional Excel export. Use the Codex bundled @oai/artifact-tool runtime.
// CSV and SQLite collection do not depend on Node.js.
import fs from 'node:fs/promises';
import path from 'node:path';
import { Workbook, SpreadsheetFile } from '@oai/artifact-tool';

const [input, output, previewDir] = process.argv.slice(2);
if (!input || !output) throw new Error('Usage: node export_excel.mjs index.json output.xlsx [preview_directory]');
try { await fs.access(output); throw new Error('Output exists; choose a new filename.'); }
catch (e) { if (e.code !== 'ENOENT') throw e; }
const data = JSON.parse(await fs.readFile(input, 'utf8'));
const browserImported = data.download.some(d=>d.transport==='imported_browser_download');
const wb = Workbook.create();
const safe = value => typeof value === 'string' && /^[\s]*[=+@-]/.test(value) ? "'" + value : value ?? '';
let tableNumber = 0;
const letters = n => n <= 26 ? String.fromCharCode(64+n) : letters(Math.floor((n-1)/26)) + letters((n-1)%26+1);

function grid(name, columns, rows) {
  const s = wb.worksheets.add(name);
  s.showGridLines = false;
  const values = [columns.map(c => c[1]), ...rows.map(r => columns.map(c => safe(r[c[0]])))];
  const range = s.getRangeByIndexes(0, 0, values.length, columns.length);
  range.values = values;
  range.format.font = {name: 'Arial', size: 10, color: '#172B4D'};
  range.format.rowHeight = 24;
  range.format.verticalAlignment = 'center';
  range.format.columnWidth = 24;
  const h = s.getRangeByIndexes(0, 0, 1, columns.length);
  h.format.fill = '#243D5C';
  h.format.font = {name: 'Arial', size: 10, bold: true, color: '#FFFFFF'};
  h.format.horizontalAlignment = 'center';
  h.format.rowHeight = 30;
  for (let i = 0; i < columns.length; i++) {
    s.getRangeByIndexes(0, i, values.length, 1).format.columnWidth = columns[i][2] || 24;
  }
  range.format.wrapText = true;
  range.format.autofitRows();
  if (rows.length) {
    s.tables.add(`A1:${letters(columns.length)}${values.length}`, true, 'Table' + (++tableNumber));
  }
  s.freezePanes.freezeRows(1);
  return s;
}

const summary = wb.worksheets.add('采集状态');
summary.showGridLines = false;
summary.getRange('A2:B2').values = [['专项债募投报告采集', `2022—2026，截至${data.summary.as_of}`]];
summary.getRange('A4:B12').values = [
  ['项目', '本次记录'],
  ['全量状态', '未完成；批次分母尚未核实'],
  ['访问状态', data.host_states?.some(h=>h.blocked) ? (browserImported ? '脚本访问受限；浏览器样本已导入' : '脚本访问受限，已停止') : '未记录持久访问拦截'],
  ['观察到的公告数', data.summary.announcements],
  ['选中的附件直链数', data.summary.selected_attachments],
  ['报告来源记录数', data.summary.reports],
  ['去重后的 PDF 数', data.summary.unique_pdfs],
  ['异常条数', data.summary.exceptions],
  ['清单性质', '当前本地索引，完整性待核实']
];
summary.getRange('A14:B16').values = [
  ['范围', '广东七市；深圳另行处理；排除广州'],
  ['计数口径', '每个“0”仅表示本地尚无记录'],
  ['来源', '详见批次与附件清单中的公告链接']
];
summary.getRange('A2:B16').format.font = {name:'Arial', size:10, color:'#172B4D'};
summary.getRange('A2').format.font = {name:'Arial', size:14, bold:true};
summary.getRange('A2:B16').format.rowHeight = 26;
summary.getRange('A2:A16').format.columnWidth = 29;
summary.getRange('B2:B16').format.columnWidth = 55;
summary.getRange('A4:B4').format.fill = '#243D5C';
summary.getRange('A4:B4').format.font = {name:'Arial',size:10,bold:true,color:'#FFFFFF'};
summary.getRange('B5:B6').format.fill = '#FFF0D1';

grid('批次清单', [['year','债券年份',13],['region','发行主体',14],['title','公告标题',70],['url','公告来源',72],
  ['珠海','珠海直链数',16],['佛山','佛山直链数',16],['惠州','惠州直链数',16],['东莞','东莞直链数',16],
  ['中山','中山直链数',16],['江门','江门直链数',16],['肇庆','肇庆直链数',16],['深圳','深圳直链数',16],
  ['inventory_status','清单完整性',46],['evidence','证据类型',33]], data.batches);
grid('附件索引', [['year','债券年份',13],['region','发行主体',14],['city','城市',12],['name','原附件名',90],
  ['status','下载状态',18],['reason','筛选依据',28],['announcement_url','公告来源',72],['url','附件直链',100],['evidence','证据类型',32]],data.attachment);
grid('报告索引', [['year','债券年份',13],['city','城市',12],['project_path','包内项目路径',65],['report_name','报告文件名',65],
  ['pdf_pages','PDF页数',14],['text_status','正文提取情况',48],['path','本地文件',80],['sha256','PDF SHA256',72],['announcement_url','公告来源',72],['attachment_url','附件来源',100],
  ['original_member','原始包内路径',85],['evidence','证据类型',32]],data.report);
grid('异常清单', [['stage','阶段',18],['code','异常类型',32],['source','来源',85],['message','详细原因',110],['at','UTC时间',38]],data.exceptions);
grid('覆盖情况', [['year','年份',12],['city','城市',12],['selected_attachments','已识别附件',18],['completed_attachments','已提取附件',18],
  ['report_occurrences','报告来源数',18],['unique_pdf_hashes','唯一PDF数',18],['coverage','完整性状态',42]],data.coverage);
wb.recalculate();
console.log((await wb.inspect({kind:'region',sheetId:'采集状态',range:'A4:B12',maxChars:3000})).ndjson);
console.log((await wb.inspect({kind:'match',searchTerm:'#REF!|#DIV/0!|#VALUE!|#NAME\\?|#NUM!|#SPILL!',options:{useRegex:true,maxResults:20},summary:'formula_error_check'})).ndjson);
if (previewDir) {
  await fs.mkdir(previewDir, {recursive:true});
  for (const [name, range] of [['采集状态','A1:B16'],['批次清单','A1:F3'],['附件索引','A1:F3'],['报告索引','A1:D3'],['异常清单','A1:B8'],['覆盖情况','A1:G12']]) {
    const img = await wb.render({sheetName:name, range, scale:1.5,format:'png'});
    await fs.writeFile(path.join(previewDir, name+'.png'),new Uint8Array(await img.arrayBuffer()));
  }
}
const file = await SpreadsheetFile.exportXlsx(wb);
await file.save(output);
console.log(JSON.stringify({xlsx:path.resolve(output),sheets:6,source:path.resolve(input)}));

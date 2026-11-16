// Builds the Tdarr flow "Downscale >1080p to 1080p H.264 (QSV), keep all audio/subs"
// and prints the JSON document. Usage (inside the tdarr container):
//   RADARR_KEY=.. SONARR_KEY=.. node build-flow.js > flow.json
const FLOW_ID = 'downscale1080';

const QSV = '-hide_banner -y -init_hw_device qsv=hw:/dev/dri/renderD128 -i "{{{args.inputFileObj._id}}}"'
  + ' -map 0 -map -0:d? -c copy {{{args.variables.user.subArgs}}}'
  + ' -c:v:0 h264_qsv -preset slow -global_quality 21 -look_ahead 0 -max_muxing_queue_size 9999';
const SDR_FILTER = 'scale=1920:-2:flags=lanczos,format=nv12';
const HDR_FILTER = 'zscale=w=1920:h=-2:filter=lanczos,zscale=t=linear:npl=100,format=gbrpf32le,'
  + 'zscale=p=bt709,tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,format=nv12';

// gate: only files wider than 1920 or taller than 1080 continue (output 1); others stop (output 2).
// Also picks subtitle handling: MP4 text subs (mov_text) can't be copied into MKV, so convert them to SRT.
const GATE = `
module.exports = async (args) => {
  const streams = args.inputFileObj.ffProbeData.streams || [];
  const v = streams.find((s) => s.codec_type === 'video' && !(s.disposition && s.disposition.attached_pic));
  const big = v && (Number(v.width) > 1920 || Number(v.height) > 1080);
  args.variables.user = args.variables.user || {};
  args.variables.user.subArgs = streams.some((s) => s.codec_name === 'mov_text') ? '-c:s srt' : '';
  args.jobLog('video ' + (v ? v.width + 'x' + v.height : 'none') + (big ? ' -> downscale' : ' -> leave as is'));
  return { outputFileObj: args.inputFileObj, outputNumber: big ? 1 : 2, variables: args.variables };
};`;

// safety: converted file must have the same number of audio and subtitle tracks as the original.
const SAME_TRACKS = `
module.exports = async (args) => {
  const { execFileSync } = require('child_process');
  const probe = (f) => JSON.parse(execFileSync('/usr/local/bin/ffprobe',
    ['-v', 'error', '-show_streams', '-of', 'json', f]).toString()).streams;
  const count = (st, t) => st.filter((s) => s.codec_type === t).length;
  const orig = probe(args.originalLibraryFile._id);
  const out = probe(args.inputFileObj._id);
  const ok = ['audio', 'subtitle'].every((t) => count(orig, t) === count(out, t));
  args.jobLog('audio ' + count(orig, 'audio') + '->' + count(out, 'audio')
    + ', subtitles ' + count(orig, 'subtitle') + '->' + count(out, 'subtitle') + (ok ? ' OK' : ' MISMATCH'));
  return { outputFileObj: args.inputFileObj, outputNumber: ok ? 1 : 2, variables: args.variables };
};`;

// after replacing: tell the right *arr to refresh that movie/series so it sees the new file.
const NOTIFY = `
module.exports = async (args) => {
  const file = args.inputFileObj._id;
  const apps = [
    { prefix: '/data/media/movies/', host: 'http://172.30.0.10:7878', key: '${process.env.RADARR_KEY}', list: 'movie', cmd: (id) => ({ name: 'RefreshMovie', movieIds: [id] }) },
    { prefix: '/data/media/tv/', host: 'http://172.30.0.11:8989', key: '${process.env.SONARR_KEY}', list: 'series', cmd: (id) => ({ name: 'RefreshSeries', seriesId: id }) },
    { prefix: '/data/media/anime/', host: 'http://172.30.0.11:8989', key: '${process.env.SONARR_KEY}', list: 'series', cmd: (id) => ({ name: 'RefreshSeries', seriesId: id }) },
  ];
  const app = apps.find((a) => file.startsWith(a.prefix));
  try {
    if (!app) throw new Error('no app for ' + file);
    const h = { 'X-Api-Key': app.key, 'Content-Type': 'application/json' };
    const items = await (await fetch(app.host + '/api/v3/' + app.list, { headers: h })).json();
    const item = items.find((i) => i.path && file.startsWith(i.path.replace(/\\/$/, '') + '/'));
    if (!item) throw new Error('no ' + app.list + ' owns ' + file);
    const r = await fetch(app.host + '/api/v3/command', { method: 'POST', headers: h, body: JSON.stringify(app.cmd(item.id)) });
    args.jobLog('refresh sent for ' + (item.title || item.id) + ': HTTP ' + r.status);
  } catch (e) {
    args.jobLog('arr refresh skipped: ' + e.message);
  }
  return { outputFileObj: args.inputFileObj, outputNumber: 1, variables: args.variables };
};`;

const p = (id, name, pluginName, x, y, inputsDB) => ({
  name, sourceRepo: 'Community', pluginName, version: '1.0.0', id,
  position: { x, y }, fpEnabled: true, ...(inputsDB ? { inputsDB } : {}),
});
const cli = (filter) => ({
  useCustomCliPath: 'true',
  customCliPath: '/usr/local/bin/tdarr-ffmpeg',
  doesCommandCreateOutputFile: 'true',
  userOutputFilePath: '${cacheDir}/${fileName}.mkv',
  cliArguments: `${QSV} -filter:v:0 "${filter}" "\${outputFilePath}"`,
  outputFileBecomesWorkingFile: 'true',
});

const flowPlugins = [
  p('in', 'Input File', 'inputFile', 400, 0),
  p('gate', 'Bigger than 1080p?', 'customFunction', 400, 120, { code: GATE }),
  p('hdr', 'Is HDR?', 'checkHdr', 400, 240),
  p('sdrcli', 'Downscale SDR (QSV H.264)', 'runCli', 200, 360, cli(SDR_FILTER)),
  p('hdrcli', 'Downscale + tonemap HDR (QSV H.264)', 'runCli', 600, 360, cli(HDR_FILTER)),
  p('dur', 'Same duration?', 'compareFileDurationRatio', 400, 480, { greaterThan: '98', lessThan: '102' }),
  p('tracks', 'Same audio/subtitle tracks?', 'customFunction', 400, 600, { code: SAME_TRACKS }),
  p('replace', 'Replace original', 'replaceOriginalFile', 400, 720),
  p('notify', 'Refresh Radarr/Sonarr', 'customFunction', 400, 840, { code: NOTIFY }),
  p('fail', 'Fail (keep original)', 'failFlow', 800, 600),
];
let n = 0;
const e = (source, sourceHandle, target) => ({ source, sourceHandle: String(sourceHandle), target, targetHandle: null, id: 'e' + (n += 1) });
const flowEdges = [
  e('in', 1, 'gate'),
  e('gate', 1, 'hdr'),          // gate output 2 (<=1080p) has no edge: flow ends, file untouched
  e('hdr', 1, 'hdrcli'),
  e('hdr', 2, 'sdrcli'),
  e('sdrcli', 1, 'dur'),
  e('hdrcli', 1, 'dur'),
  e('dur', 1, 'tracks'),
  e('dur', 2, 'fail'),
  e('dur', 3, 'fail'),
  e('tracks', 1, 'replace'),
  e('tracks', 2, 'fail'),
  e('replace', 1, 'notify'),
];

process.stdout.write(JSON.stringify({
  _id: FLOW_ID,
  name: 'Downscale >1080p to 1080p H.264 (QSV), keep all audio/subs',
  description: 'Files over 1080p become 1080p H.264 via Intel QSV (HDR tone-mapped to SDR). All audio and subtitle tracks copied. Original replaced only if duration and track counts match.',
  tags: '',
  flowPlugins,
  flowEdges,
}));

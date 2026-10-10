// Private intel tool failures use the executor's supported HarnessError protocol.
import { HarnessError } from '@deepseek-ai/dsh-llm';
import {JsonStoreError} from './json-store.js';

const messages = {
 CRON_INVALID_SCHEDULE:'时间描述无效；请使用每天9点、每5分钟或30分钟后等支持的说法。',
 CRON_INVALID_NAME:'任务名称不能为空。',
 CRON_INVALID_PROMPT:'任务内容（prompt）不能为空。',
 CRON_INVALID_INTERVAL:'间隔太短或无效；最小支持每 5 分钟一次。',
 CRON_INVALID_REF:'请用 cron_list 查看任务并指定 id。',
 CRON_NOT_FOUND:'没找到任务；请用 cron_list 查看现有 id。',
 CRON_AMBIGUOUS_REF:'名称命中多个任务；请用 cron_list 查看并指定 id。',
 CRON_CAPACITY_REACHED:'定时任务已达上限（20 个）；请先删除不用的任务。',
 CRON_SESSION_REQUIRED:'cron 工具需要在会话内调用。',
 CRON_OPERATION_FAILED:'定时任务操作失败；结果未确认，请读取确认，勿自动重放变更。',
 GOALS_INVALID_TITLE:'goal_create: title 不能为空。',
 GOALS_INVALID_PROGRESS:'goal_progress: text 不能为空。',
 GOALS_INVALID_REF:'引用为空；请用 goal_list 查看现有目标并指定 id。',
 GOALS_NOT_FOUND:'找不到目标；请用 goal_list 查看现有目标并指定 id。',
 GOALS_AMBIGUOUS_REF:'匹配到多个目标；请用 goal_list 查看并指定 id 精确指定。',
 GOALS_ALREADY_CLOSED:'目标已经关闭；请用 goal_list 确认状态。',
 GOALS_INVALID_STATUS:'status 只能是 active/closed/all。',
 GOALS_OPERATION_FAILED:'目标操作失败；结果未确认，请读取确认，勿自动重放变更。',
 GOALS_PROJECTION_FAILED:'Markdown 未同步；请读取 JSON 确认并修复投影，勿重复业务变更。',
 MEMORY_INVALID_TEXT:'memory text must be a non-empty string。',
 MEMORY_WRITE_FAILED:'记忆写入失败；结果未确认，请查询确认，勿自动重放写入。',
 MEMORY_READ_FAILED:'记忆读取失败；请检查来源可读性。',
 MEMORY_SEARCH_FAILED:'记忆搜索失败；请检查来源后重新查询。',
 MEMORY_QUERY_TOO_LARGE:'查询超过配置上限；请缩短查询并明确主题。',
 MEMORY_RECALL_TOO_LARGE:'候选记忆超过配置字节上限；请使用更具体的查询。',
 SYSEVENTS_INVALID_ENTRY:'事件参数无效；请检查 type、title 和事件身份。',
 SYSEVENTS_IDENTITY_CONFLICT:'事件身份冲突；请读取已有事件确认。',
 SYSEVENTS_WRITE_FAILED:'事件写入失败；结果未确认，请读取确认，勿自动重放发射。',
 SYSEVENTS_READ_FAILED:'事件读取失败；请检查来源可读性。',
 SYSEVENTS_INVALID_DATA:'事件源损坏；请修复来源后读取。',
 SYSEVENTS_SOURCE_TOO_LARGE:'事件源超过读取上限。',
};
for(const prefix of ['CRON','GOALS'])for(const kind of ['READ_FAILED','WRITE_FAILED','INVALID_DATA','LOCK_FAILED','LOCK_TIMEOUT','ID_EXHAUSTED','SCHEDULE_FAILED']){
 messages[prefix+'_'+kind] ??= '持久化失败；请读取确认并检查存储状态，勿自动重放变更。';
}
// The worker uses these exact safe messages as its established process failure protocol.
const tokenlogCodes=['TOKENLOG_CANCELLED','TOKENLOG_TIMEOUT','TOKENLOG_SOURCE_UNAVAILABLE','TOKENLOG_ZSTD_UNAVAILABLE','TOKENLOG_ZSTD_NOT_EXECUTABLE','TOKENLOG_DECOMPRESS_FAILED','TOKENLOG_LOG_INVALID','TOKENLOG_LOG_INVALID_UTF8','TOKENLOG_LOG_UNREADABLE','TOKENLOG_LOG_MISSING','TOKENLOG_DUPLICATE_CONFLICT','TOKENLOG_METADATA_UNREADABLE','TOKENLOG_METADATA_INVALID','TOKENLOG_READ_FAILED'];
for(const code of tokenlogCodes)messages[code]='用量读取失败；数据不可用，请修复来源或重新发起读取。';
const errnos=new Set(['EACCES','EPERM','EROFS','ENOSPC','EDQUOT','EIO','ENOENT','ENOTDIR','EISDIR','EEXIST','EBUSY','EMFILE','ENFILE','SQLITE_BUSY','SQLITE_LOCKED','ERR_SQLITE_ERROR']);
class IntelToolError extends HarnessError {}

/** Create an owner-classified failure with fixed safe text; raw exception bodies are never rendered. */
export function intelError(code){
 if(!Object.hasOwn(messages,code))throw new TypeError('Unknown intel failure code');
 return new IntelToolError(`${code}：${messages[code]}`,code);
}

/** Preserve supported owner codes and proven commit state in both structured and rendered failures. */
export function toolError(error,fallback,{availableText}={}){
 const code=Object.hasOwn(messages,error?.code)?error.code:fallback;
 const failure=intelError(code);
 if(code==='CRON_CAPACITY_REACHED'&&error instanceof IntelToolError&&Number.isSafeInteger(error.used)&&error.used>=20)failure.message+=` 已用 ${error.used}/20`;
 const errno=error instanceof JsonStoreError&&errnos.has(error.errno)?error.errno:undefined;
 if(errno)failure.message+=` errno=${errno}`;
 if(error instanceof JsonStoreError)failure.message+=` committed=${error.committed}${error.committed?'；JSON 已提交；请读取确认，不要重复变更。':''}`;
 if(availableText!==undefined)failure.message+=`\nMarkdown 未同步；JSON 目标可读（部分可用）：\n${availableText}`;
 return failure;
}

/** Project only exact worker protocol failures; arbitrary worker messages remain private. */
export function tokenlogToolError(error){
 return intelError(tokenlogCodes.includes(error?.message)?error.message:'TOKENLOG_READ_FAILED');
}

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { openStore } from '../src/store.js';
import { FtsRetriever } from '../src/retriever.js';

// Synthetic regression examples authored independently, then used to refine the ranking.
// Labels are test judgments, never stored kinds.
const rows = [
  ['release-birch', '白桦批次发布记录：说明方式检查完成，先给结论模板已安装；发布、部署、说明方式、结论检查全部通过。'],
  ['coffee-fact', '榆林展馆的咖啡柜台出售桂花拿铁，周末下午营业。'],
  ['client-fact', '霁川系统的雨燕终端是展示客户端，呈现会话、工具和任务状态。'],
  ['theme-preference', '我偏好深紫色主题，阅读长文时眼睛更舒服。'],
  ['release-willow', '柳岸r8发布记录：说明方式采用先给结论模板，发布与部署检查完成；说明方式、结论、模板校验通过。'],
  ['habit-fact', '榆林公园工作日清晨开放散步步道，东门六点开放。'],
  ['reply-preference', '我希望答复先给结论，接着解释理由，说明方式尽量简洁。'],
  ['connection-fact', '霁川系统的雨燕终端通过加密连接获取会话、工具和任务状态。'],
  ['release-maple', '枫桥批次发布记录：发布检查、部署检查及说明方式检查已完成，先给结论模板复核通过。'],
  ['coffee-preference', '我喜欢桂花拿铁，点咖啡时要少冰。'],
  ['theme-fact', '织锦编辑器的深紫色主题由松塔设计组制作，主题资源包有十二个图标。'],
  ['state-owner', '霁川系统由苍鹭控制台负责管理模型、会话、工具和任务状态；雨燕终端只展示这些状态。'],
  ['release-elm', '榆桥批次发布记录：说明方式模板复核、部署校验、发布校验均完成，先给结论规则检查通过。'],
  ['reminder-preference', '我偏好提醒合并成每天一次的摘要，不要每条通知都打断我。'],
  ['deployment-fact', '霁川系统的苍鹭控制台部署在松塔机房，雨燕终端安装包从内部镜像分发。'],
  ['walk-preference', '我希望工作日清晨散步半小时，再开始处理邮件。'],
  ['chinese-subject', '青色琥珀棱镜的折射校准记录保存在织锦档案库。'],
  ['latin-subject', 'marigold spectrometer alignment notebook: reference measurements are stored in drawer seven.'],
  ['prefix-noise', '前面提到的说明和安排已经看过，之前项目的聊天摘要已归档。'],
  ['camera-fact', 'camera handbook for technicians: exposure calibration procedures.'],
];

const preferenceKeys = new Set([
  'reply-preference', 'theme-preference', 'coffee-preference',
  'reminder-preference', 'walk-preference',
]);

function corpus(t, insertionOrder = rows) {
  const directory = mkdtempSync(join(tmpdir(), 'memory-quality-'));
  let retriever;
  t.after(() => {
    try { retriever?.close(); }
    finally { rmSync(directory, { recursive: true, force: true }); }
  });
  retriever = new FtsRetriever(openStore(directory));
  const keysById = new Map();
  for (const [key, text] of insertionOrder) {
    const { id } = retriever.add({ text, kind: 'note' });
    keysById.set(id, key);
  }
  return {
    search(query, limit) {
      const hits = retriever.search(query, limit);
      assert.ok(hits.length <= limit, 'Results must honor the requested limit');
      assert.equal(new Set(hits.map(hit => hit.id)).size, hits.length, 'Results must be distinct');
      return hits.map(hit => {
        assert.ok(keysById.has(hit.id), 'Every result must refer to an inserted row');
        return keysById.get(hit.id);
      });
    },
  };
}

for (const query of [
  '我的说明方式偏好是什么，答复应该先给结论吗？',
  '我希望回答先给结论还是先解释理由？',
]) {
  test(`specific preference excludes repetitive release checks: ${query}`, t => {
    const memory = corpus(t);
    for (const limit of [3, 5]) {
      const keys = memory.search(query, limit);
      assert.equal(keys[0], 'reply-preference', `The personal preference must lead: ${JSON.stringify(keys)}`);
      assert.ok(keys.every(key => preferenceKeys.has(key)), `Release checks do not answer a preference question: ${JSON.stringify(keys)}`);
    }
  });
}

for (const query of ['柳岸r8发布记录', '查一下柳岸r8的发布与部署检查记录']) {
  test(`named release query retains its operational log: ${query}`, t => {
    const memory = corpus(t);
    for (const limit of [3, 5]) {
      assert.equal(memory.search(query, limit)[0], 'release-willow');
    }
  });
}

const orders = [
  ['mixed', rows],
  ['reshuffled', [7, 15, 2, 18, 9, 0, 12, 5, 16, 4, 13, 1, 19, 17, 10, 8, 14, 6, 11, 3].map(index => rows[index])],
];

for (const [orderName, insertionOrder] of orders) {
  for (const query of ['我有哪些偏好？', '我喜欢什么，有哪些希望？', '请回忆一下我的个人喜好']) {
    for (const limit of [3, 5]) {
      test(`broad preferences fill ${limit} slots with acceptable distinct preferences (${orderName}): ${query}`, t => {
        const memory = corpus(t, insertionOrder);
        const keys = memory.search(query, limit);
        assert.equal(keys.length, limit, `Five independent preferences are available: ${JSON.stringify(keys)}`);
        assert.ok(keys.every(key => preferenceKeys.has(key)), `Every returned row must answer the preference question: ${JSON.stringify(keys)}`);
      });
    }
  }
}

for (const query of [
  '谁负责管理霁川系统的会话、工具和任务状态？',
  '霁川系统的模型和任务状态由谁管理？',
  '苍鹭控制台和雨燕终端各有什么职责，谁负责会话状态？',
]) {
  test(`responsibility recall includes the state manager among adjacent system facts: ${query}`, t => {
    const memory = corpus(t);
    for (const limit of [3, 5]) {
      const keys = memory.search(query, limit);
      assert.ok(keys.includes('state-owner'), `Connection, display and deployment facts cannot replace responsibility: ${JSON.stringify(keys)}`);
    }
  });
}

for (const { query, required } of [
  { query: '桂花拿铁咖啡', required: ['coffee-preference', 'coffee-fact'] },
  { query: '深紫色主题', required: ['theme-preference', 'theme-fact'] },
  { query: '工作日清晨散步', required: ['walk-preference', 'habit-fact'] },
]) {
  test(`generic topic retains both preferences and relevant non-preference facts: ${query}`, t => {
    const memory = corpus(t);
    for (const limit of [3, 5]) {
      const keys = memory.search(query, limit);
      for (const key of required) assert.ok(keys.includes(key), `Missing ${key}: ${JSON.stringify(keys)}`);
    }
  });
}

test('conversational 希望 retains a factual request for coffee counter opening hours', t => {
  const memory = corpus(t);
  for (const limit of [3, 5]) {
    const keys = memory.search('我希望了解桂花拿铁咖啡柜台的营业时间', limit);
    assert.ok(keys.includes('coffee-fact'), `A request to learn facts must retain the opening-hours fact: ${JSON.stringify(keys)}`);
  }
});

test('conversational 希望回答 retains a named release-result request', t => {
  const memory = corpus(t);
  for (const limit of [3, 5]) {
    const keys = memory.search('我希望回答柳岸r8发布与部署检查的结果', limit);
    assert.ok(keys.includes('release-willow'), `A request for release results must retain the named release log: ${JSON.stringify(keys)}`);
  }
});

for (const { query, required } of [
  { query: '青', required: 'chinese-subject' },
  { query: 'for', required: 'camera-fact' },
]) {
  test(`literal short queries retain memories containing the queried text: ${query}`, t => {
    const memory = corpus(t);
    for (const limit of [3, 5]) {
      const keys = memory.search(query, limit);
      assert.ok(keys.includes(required), `A literal query must retain its matching memory: ${JSON.stringify(keys)}`);
    }
  });
}

const releaseChecks = Array.from({ length: 160 }, (_, index) => [
  `release-check-${index}`,
  `紫藤v${index + 1}发布检查：偏好设置模板校验已完成，希望字段通过检查；偏好配置、发布记录及部署校验全部通过。`,
]);
const personalPreferences = rows.filter(([key]) => preferenceKeys.has(key));

for (const [orderName, insertionOrder] of [
  ['release checks first', [...releaseChecks, ...personalPreferences]],
  ['preferences first', [...personalPreferences, ...releaseChecks.toReversed()]],
]) {
  for (const limit of [3, 5]) {
    test(`broad preference recall survives 160 repetitive release checks at limit ${limit} (${orderName})`, t => {
      const memory = corpus(t, insertionOrder);
      const keys = memory.search('我有哪些个人偏好和喜好？', limit);
      assert.equal(keys.length, limit, `Five distinct preferences are available despite many release checks: ${JSON.stringify(keys)}`);
      assert.ok(keys.every(key => preferenceKeys.has(key)), `Release-check volume must not replace personal preferences: ${JSON.stringify(keys)}`);
    });
  }
  test(`named operational preference-settings query retains its release log (${orderName})`, t => {
    const memory = corpus(t, insertionOrder);
    for (const limit of [3, 5]) {
      const keys = memory.search('查询紫藤v160发布检查中的用户偏好设置模板记录', limit);
      assert.equal(keys[0], 'release-check-159', `A named settings-template request must lead with its release record: ${JSON.stringify(keys)}`);
    }
  });
}

for (const query of ['请帮我回忆海豚声呐研究', 'my preferences for glacier seismology']) {
  test(`unrelated conversational topic returns no memories: ${query}`, t => {
    const memory = corpus(t);
    for (const limit of [3, 5]) assert.deepEqual(memory.search(query, limit), []);
  });
}

for (const { query, required } of [
  {
    query: '前面讨论的事情先放一边，请根据之前所有聊天帮我查一下琥珀棱镜折射校准记录',
    required: 'chinese-subject',
  },
  {
    query: 'please review everything mentioned earlier in our conversation and find the marigold spectrometer alignment notebook',
    required: 'latin-subject',
  },
]) {
  test(`conversational prefixes retain the actual subject: ${query}`, t => {
    const memory = corpus(t);
    for (const limit of [3, 5]) {
      const keys = memory.search(query, limit);
      assert.equal(keys[0], required, `The requested subject must lead: ${JSON.stringify(keys)}`);
    }
  });
}

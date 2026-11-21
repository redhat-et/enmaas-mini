'use strict';
const assert = require('node:assert/strict');
const collectUsers = require('./management-users.cjs');
(async()=>{
 // The source-pinned metering build returns {data, has_more}; the walk must
 // advance the offset by the number of rows received. Pages are keyed by the
 // exact request, so a repeated or unexpected offset fails the request itself.
 const pages = {'/api/v1/management/users?limit=500&offset=0':
                  {data: [{user_id: 'one'}, {user_id: 'two'}], has_more: true},
                '/api/v1/management/users?limit=500&offset=2':
                  {data: [{user_id: 'three'}], has_more: false}};
 const urls = [];
 const envelope = async url => {
  urls.push(url);
  const page = pages[url];
  if (!page) throw new Error('unexpected page request: ' + url);
  delete pages[url];
  return {ok: true, status: 200, json: async () => page};
 };
 assert.deepEqual(await collectUsers(envelope),
                   [{user_id: 'one'}, {user_id: 'two'}, {user_id: 'three'}]);
 assert.deepEqual(urls, ['/api/v1/management/users?limit=500&offset=0',
                         '/api/v1/management/users?limit=500&offset=2']);
 // An empty page ends the walk even when has_more is still set.
 assert.deepEqual(await collectUsers(
     async () => ({ok: true, status: 200, json: async () => ({data: [], has_more: true})})), []);
 // The published image returns a bare array in a single response.
 const bare = [{user_id: 'one'}, {user_id: 'two'}];
 let calls = 0;
 const barePage = async () => {calls++; return {ok: true, status: 200, json: async () => bare};};
 assert.deepEqual(await collectUsers(barePage), bare);
 assert.equal(calls, 1);
 // HTTP failures and non-conforming bodies fail with actionable errors.
 await assert.rejects(collectUsers(async () => ({ok: false, status: 401})), /HTTP 401/);
 await assert.rejects(
     collectUsers(async () => ({ok: true, status: 200, json: async () => ({error: 'unauthorized'})})),
     /neither an array nor a paginated envelope/);
 console.log('PASS: management users walk accepts the paginated envelope and the bare array');
})().catch(error=>{console.error(error);process.exitCode=1});

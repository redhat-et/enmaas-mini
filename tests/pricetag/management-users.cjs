'use strict';
// GET /api/v1/management/users returns a paginated {data, has_more} envelope on
// the standalone-management source pin and a bare array on the published image.
// Walk every page, accepting both shapes, and return the concatenated rows.
// Self-contained on purpose: browser suites pass this function directly to
// page.evaluate, which runs it in the page context; the fetcher parameter
// defaults to that page's fetch and lets unit tests inject a stub.
module.exports = async function collectUsers(fetchPage = fetch) {
 const rows=[];let offset=0;
 for(;;){
  const response=await fetchPage('/api/v1/management/users?limit=500&offset='+offset);
  if(!response.ok)throw new Error('management users listing failed with HTTP '+response.status);
  const page=await response.json();
  const pageRows=Array.isArray(page)?page:page&&page.data;
  if(!Array.isArray(pageRows))throw new Error('management users listing is neither an array nor a paginated envelope');
  rows.push(...pageRows);
  if(Array.isArray(page)||!page.has_more||!pageRows.length)break;
  offset+=pageRows.length;
 }
 return rows;
};

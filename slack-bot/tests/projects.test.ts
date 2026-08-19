/**
 * The channel-to-project map, and the ticket query it feeds.
 *
 * Both halves fail the same silent way. A map that parses to empty looks exactly like
 * one nobody configured, and a query that quietly stops scoping looks exactly like a
 * project with a lot of tickets — so the ticket somebody wants is missing from the
 * picker and nothing anywhere says why.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';

import { readProjects, resetProjects } from '../src/projects.js';
import { searchQuery } from '../src/youtrack.js';

test('several channels can be one project, and the label is optional', () => {
  const map = readProjects('REVERAPP (Rever App)=C0ABC,C0DEF; MOB=C0GHI');
  assert.equal(map.byChannel.get('C0ABC'), 'REVERAPP');
  assert.equal(map.byChannel.get('C0DEF'), 'REVERAPP');
  assert.equal(map.byChannel.get('C0GHI'), 'MOB');
  assert.equal(map.labels.get('REVERAPP'), 'Rever App');
  assert.equal(map.labels.get('MOB'), undefined, 'no label is not an empty label');
});

test('a channel name is stored as a name, so it can be matched against one', () => {
  const map = readProjects('REVERAPP=#reverapp-dev,C0ABC');
  assert.equal(map.byName.get('#reverapp-dev'), 'REVERAPP');
  assert.equal(map.byChannel.get('#reverapp-dev'), undefined);
});

test('one broken group does not empty the whole map', () => {
  const map = readProjects('REVERAPP=C0ABC; nonsense; MOB=C0GHI');
  assert.equal(map.byChannel.get('C0ABC'), 'REVERAPP');
  assert.equal(map.byChannel.get('C0GHI'), 'MOB');
});

test('projectOf resolves by id and by name, and returns empty when unmapped', async () => {
  process.env.TAM_CHANNEL_PROJECTS = 'REVERAPP=C0ABC,#reverapp-qa';
  resetProjects();
  const { projectOf, labelOf } = await import('../src/projects.js');
  assert.equal(projectOf({ id: 'C0ABC' }), 'REVERAPP');
  assert.equal(projectOf({ name: 'reverapp-qa' }), 'REVERAPP', 'a slash command knows the name, not the id');
  assert.equal(projectOf({ id: 'C0UNKNOWN' }), '', 'unmapped is no scope, never a guessed project');
  assert.equal(labelOf('REVERAPP'), 'REVERAPP', 'with no label given, the key is what a person sees');
  delete process.env.TAM_CHANNEL_PROJECTS;
  resetProjects();
});

test('a ticket key is searched as an identity, not as text', () => {
  assert.equal(searchQuery('REV-1421', ['REV']), 'issue id: REV-1421');
  assert.equal(searchQuery('  rev-1421 ', ['REV']), 'issue id: REV-1421');
});

test('a bare number needs exactly one project to be unambiguous', () => {
  assert.equal(searchQuery('1421', ['REV']), 'issue id: REV-1421');
  assert.ok(!searchQuery('1421', ['REV', 'MOB']).includes('issue id:'));
});

test('free text stays scoped to the channel’s project', () => {
  const q = searchQuery('redemption', ['REVERAPP']);
  assert.ok(q.startsWith('project: REVERAPP'), 'scope first, or the whole tracker comes back');
  assert.ok(q.includes('redemption'));
});

test('an empty query is the project’s most recent tickets, not an error', () => {
  assert.equal(searchQuery('', ['REV']), 'project: REV sort by: updated desc');
  assert.equal(searchQuery('', []), 'sort by: updated desc');
});

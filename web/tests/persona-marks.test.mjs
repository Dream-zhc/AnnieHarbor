import assert from 'node:assert/strict';
import test from 'node:test';
import {personaMark} from '../src/persona-marks.ts';

const profiles = [{persona_id: 'annie', label: '机灵'}, {persona_id: 'annie-savage', label: '毒舌'}];

test('each reply shows its personality and a switch is marked once', () => {
  const turns = [{persona_style: 'annie'}, {persona_style: 'annie'},
    {persona_style: 'annie-savage'}, {persona_style: 'annie-savage'}];
  assert.deepEqual(turns.map((turn, index) => personaMark(turn, turns[index - 1], profiles)), [
    {label: '机灵', switched: false}, {label: '机灵', switched: false},
    {label: '毒舌', switched: true}, {label: '毒舌', switched: false},
  ]);
});

test('turns from before styles were recorded show no label or divider', () => {
  assert.deepEqual(personaMark({persona_style: null}, {persona_style: 'annie'}, profiles),
    {label: null, switched: false});
  assert.deepEqual(personaMark({persona_style: 'annie'}, {}, profiles), {label: '机灵', switched: false});
  assert.deepEqual(personaMark({persona_style: 'retired'}, {persona_style: 'annie'}, profiles),
    {label: null, switched: false});
});

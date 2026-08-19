import { describe, expect, it } from 'vitest';
import {
  mergeConsecutiveTranscripts,
  retainTranscript,
} from '../src/conversation-transcripts.ts';

describe('conversation transcripts', () => {
  it('combines consecutive user fragments into one readable turn', () => {
    expect(
      mergeConsecutiveTranscripts([
        { id: 'user-1', speaker: 'user', text: 'Tell me something more. When is the', isFinal: true },
        { id: 'user-2', speaker: 'user', text: 'next festival', isFinal: true },
        { id: 'user-3', speaker: 'user', text: 'coming in India?', isFinal: true },
        { id: 'assistant-1', speaker: 'assistant', text: 'Diwali is coming.', isFinal: true },
      ]),
    ).toEqual([
      {
        id: 'user-1',
        speaker: 'user',
        text: 'Tell me something more. When is the next festival coming in India?',
        isFinal: true,
      },
      { id: 'assistant-1', speaker: 'assistant', text: 'Diwali is coming.', isFinal: true },
    ]);
  });

  it('keeps speaker changes as separate conversation turns', () => {
    expect(
      mergeConsecutiveTranscripts([
        { id: 'user-1', speaker: 'user', text: 'Hello', isFinal: true },
        { id: 'assistant-1', speaker: 'assistant', text: 'Hi there.', isFinal: true },
        { id: 'user-2', speaker: 'user', text: 'How are you?', isFinal: true },
      ]),
    ).toHaveLength(3);
  });

  it('does not insert a space before punctuation-only fragments', () => {
    expect(
      mergeConsecutiveTranscripts([
        { id: 'user-1', speaker: 'user', text: 'Wait', isFinal: true },
        { id: 'user-2', speaker: 'user', text: ', actually.', isFinal: true },
      ])[0]?.text,
    ).toBe('Wait, actually.');
  });

  it('retains only the latest configured transcript segments', () => {
    const transcripts = new Map();
    retainTranscript(transcripts, { id: '1', speaker: 'user', text: 'One', isFinal: true }, 2);
    retainTranscript(transcripts, { id: '2', speaker: 'assistant', text: 'Two', isFinal: true }, 2);
    retainTranscript(transcripts, { id: '3', speaker: 'user', text: 'Three', isFinal: true }, 2);

    expect([...transcripts.keys()]).toEqual(['2', '3']);
  });
});

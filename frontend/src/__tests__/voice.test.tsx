import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { VoiceControl } from '../components/voice/VoiceControl';
import { api } from '../api/client';

vi.mock('../api/client', () => ({
  api: {
    getPerceptionStatus: vi.fn(),
    startAudioSession: vi.fn(),
    stopAudioSession: vi.fn(),
    cancelAudioSession: vi.fn(),
  },
}));

describe('VoiceControl Push-to-Talk Lifecycle', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('cancels the tracked session on unmount even if cancellation fails', async () => {
    vi.mocked(api.getPerceptionStatus).mockResolvedValueOnce({
      enabled: true, audio_capture_enabled: true, vision_enabled: false,
      motion_enabled: false, environmental_enabled: false, adapter_mode: 'mock',
      active_audio_sessions: 0,
    });
    vi.mocked(api.startAudioSession).mockResolvedValueOnce({
      session_id: 'unmounted_session', status: 'active',
    });
    vi.mocked(api.cancelAudioSession).mockRejectedValueOnce(new Error('Backend unavailable'));
    const onTranscription = vi.fn();
    const { unmount } = render(<VoiceControl onTranscription={onTranscription} />);
    const start = screen.getByLabelText('Start voice capture session');
    await waitFor(() => expect(start).not.toBeDisabled());
    fireEvent.click(start);
    await screen.findByLabelText('Stop recording and transcribe');
    unmount();
    await waitFor(() => expect(api.cancelAudioSession).toHaveBeenCalledWith('unmounted_session'));
    expect(onTranscription).not.toHaveBeenCalled();
  });

  it('renders disabled state when perception subsystem is unavailable', async () => {
    vi.mocked(api.getPerceptionStatus).mockResolvedValueOnce({
      enabled: false,
      audio_capture_enabled: false,
      vision_enabled: false,
      motion_enabled: false,
      environmental_enabled: false,
      adapter_mode: 'disabled',
      active_audio_sessions: 0,
    });

    render(<VoiceControl onTranscription={vi.fn()} />);

    const micBtn = screen.getByLabelText('Start voice capture session');
    await waitFor(() => {
      expect(micBtn).toBeDisabled();
    });

    // Confirms it never begins capture automatically
    expect(api.startAudioSession).not.toHaveBeenCalled();
  });

  it('initiates explicit capture session with user consent on click and transcribes upon stop', async () => {
    vi.mocked(api.getPerceptionStatus).mockResolvedValueOnce({
      enabled: true,
      audio_capture_enabled: true,
      vision_enabled: false,
      motion_enabled: false,
      environmental_enabled: false,
      adapter_mode: 'mock',
      active_audio_sessions: 0,
    });

    vi.mocked(api.startAudioSession).mockResolvedValueOnce({
      session_id: 'audio_sess_123',
      status: 'active',
    });

    vi.mocked(api.stopAudioSession).mockResolvedValueOnce({
      session_id: 'audio_sess_123',
      transcription: 'Schedule deployment at midnight',
      status: 'stopped',
    });

    const onTranscription = vi.fn();
    render(<VoiceControl onTranscription={onTranscription} />);

    const micBtn = await screen.findByLabelText('Start voice capture session');
    expect(micBtn).not.toBeDisabled();

    // Click to start capture
    fireEvent.click(micBtn);

    await waitFor(() => {
      expect(api.startAudioSession).toHaveBeenCalledWith(true);
    });

    // Recording state rendered with stop button
    const stopBtn = await screen.findByLabelText('Stop recording and transcribe');
    expect(stopBtn).toBeInTheDocument();

    // Click stop to process speech
    fireEvent.click(stopBtn);

    await waitFor(() => {
      expect(api.stopAudioSession).toHaveBeenCalledWith('audio_sess_123');
      expect(onTranscription).toHaveBeenCalledWith('Schedule deployment at midnight');
    });
  });

  it('handles cancellation cleanly without persisting or transcribing audio', async () => {
    vi.mocked(api.getPerceptionStatus).mockResolvedValueOnce({
      enabled: true,
      audio_capture_enabled: true,
      vision_enabled: false,
      motion_enabled: false,
      environmental_enabled: false,
      adapter_mode: 'mock',
      active_audio_sessions: 0,
    });

    vi.mocked(api.startAudioSession).mockResolvedValueOnce({
      session_id: 'audio_sess_999',
      status: 'active',
    });

    vi.mocked(api.cancelAudioSession).mockResolvedValueOnce({
      session_id: 'audio_sess_999',
      status: 'cancelled',
    });

    const onTranscription = vi.fn();
    render(<VoiceControl onTranscription={onTranscription} />);

    const micBtn = await screen.findByLabelText('Start voice capture session');
    fireEvent.click(micBtn);

    const cancelBtn = await screen.findByLabelText('Cancel recording');
    fireEvent.click(cancelBtn);

    await waitFor(() => {
      expect(api.cancelAudioSession).toHaveBeenCalledWith('audio_sess_999');
      expect(onTranscription).not.toHaveBeenCalled();
    });
  });
});

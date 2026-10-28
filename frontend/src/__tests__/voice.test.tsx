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
    getAudioSessionStatus: vi.fn(),
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

  it('locks controls on ambiguous stop failure, prevents repeated mutations, and unlocks only on authoritative completion', async () => {
    vi.mocked(api.getPerceptionStatus).mockResolvedValue({
      enabled: true,
      audio_capture_enabled: true,
      vision_enabled: false,
      motion_enabled: false,
      environmental_enabled: false,
      adapter_mode: 'mock',
      active_audio_sessions: 1,
    });

    vi.mocked(api.startAudioSession).mockResolvedValueOnce({
      session_id: 'audio_sess_ambig_1',
      status: 'active',
    });

    // Network error on stop
    const netErr = new Error('Network interrupted during stop');
    netErr.name = 'NetworkError';
    vi.mocked(api.stopAudioSession).mockRejectedValueOnce(netErr);

    render(<VoiceControl onTranscription={vi.fn()} />);

    // Start
    const micBtn = await screen.findByLabelText('Start voice capture session');
    fireEvent.click(micBtn);

    // Stop -> network error
    const stopBtn = await screen.findByLabelText('Stop recording and transcribe');
    fireEvent.click(stopBtn);

    // Outcome unknown notice appears, and repeat mutations are disabled
    await waitFor(() => {
      expect(screen.getByText(/Outcome unknown/i)).toBeInTheDocument();
    });

    // Both stop and cancel are disabled while ambiguous!
    expect(stopBtn).toBeDisabled();
    const cancelBtn = screen.getByLabelText('Cancel recording');
    expect(cancelBtn).toBeDisabled();

    // Reconcile button appears
    const reconcileBtn = screen.getByLabelText('Reconcile audio session');
    expect(reconcileBtn).toBeInTheDocument();

    // 1. Inconclusive reconciliation: backend says session is still 'capturing'
    vi.mocked(api.getAudioSessionStatus).mockResolvedValueOnce({
      session_id: 'audio_sess_ambig_1',
      state: 'capturing',
      started_at: new Date().toISOString(),
      completed_at: null,
      microphone_active: false,
      source_device: 'mic:local',
    });

    fireEvent.click(reconcileBtn);

    await waitFor(() => {
      expect(screen.getByText(/Reconciliation inconclusive/i)).toBeInTheDocument();
    });
    // Controls MUST remain locked!
    expect(stopBtn).toBeDisabled();
    expect(cancelBtn).toBeDisabled();

    // 2. Authoritative resolution: backend says session is 'completed'
    vi.mocked(api.getAudioSessionStatus).mockResolvedValueOnce({
      session_id: 'audio_sess_ambig_1',
      state: 'completed',
      started_at: new Date().toISOString(),
      completed_at: new Date().toISOString(),
      microphone_active: false,
      source_device: 'mic:local',
    });

    fireEvent.click(reconcileBtn);

    await waitFor(() => {
      expect(screen.getByTitle(/Audio session stop confirmed by server/i)).toBeInTheDocument();
    });

    // Recording state ended, mic button available again
    expect(screen.getByLabelText('Start voice capture session')).toBeInTheDocument();
    expect(screen.queryByLabelText('Stop recording and transcribe')).not.toBeInTheDocument();
  });

  it('locks controls on ambiguous cancel failure and treats missing session as inconclusive without fabricating certainty', async () => {
    vi.mocked(api.getPerceptionStatus).mockResolvedValue({
      enabled: true,
      audio_capture_enabled: true,
      vision_enabled: false,
      motion_enabled: false,
      environmental_enabled: false,
      adapter_mode: 'mock',
      active_audio_sessions: 1,
    });

    vi.mocked(api.startAudioSession).mockResolvedValueOnce({
      session_id: 'audio_sess_ambig_2',
      status: 'active',
    });

    const netErr = new Error('Network timeout');
    netErr.name = 'TimeoutError';
    vi.mocked(api.cancelAudioSession).mockRejectedValueOnce(netErr);

    render(<VoiceControl onTranscription={vi.fn()} />);

    // Start
    const micBtn = await screen.findByLabelText('Start voice capture session');
    fireEvent.click(micBtn);

    // Cancel -> timeout error
    const cancelBtn = await screen.findByLabelText('Cancel recording');
    fireEvent.click(cancelBtn);

    // Outcome unknown notice appears, and repeat mutations are disabled
    await waitFor(() => {
      expect(screen.getByText(/Outcome unknown/i)).toBeInTheDocument();
    });

    const stopBtn = screen.getByLabelText('Stop recording and transcribe');
    expect(stopBtn).toBeDisabled();
    expect(cancelBtn).toBeDisabled();

    // Reconcile: backend returns 404 (session not found due to backend restart)
    vi.mocked(api.getAudioSessionStatus).mockRejectedValueOnce(new Error('AudioSession audio_sess_ambig_2 not found (HTTP 404)'));

    const reconcileBtn = screen.getByLabelText('Reconcile audio session');
    fireEvent.click(reconcileBtn);

    // MUST NOT fabricate certainty that missing session == cancelled!
    await waitFor(() => {
      expect(screen.getByText(/Reconciliation inconclusive/i)).toBeInTheDocument();
      expect(screen.getByTitle(/not found on server/i)).toBeInTheDocument();
    });

    // Controls remain locked to prevent unconfirmed state assumptions
    expect(stopBtn).toBeDisabled();
    expect(cancelBtn).toBeDisabled();

    // Now server returns authoritative cancelled status
    vi.mocked(api.getAudioSessionStatus).mockResolvedValueOnce({
      session_id: 'audio_sess_ambig_2',
      state: 'cancelled',
      started_at: new Date().toISOString(),
      completed_at: new Date().toISOString(),
      microphone_active: false,
      source_device: 'mic:local',
    });

    fireEvent.click(reconcileBtn);

    await waitFor(() => {
      expect(screen.getByTitle(/Audio session cancellation confirmed by server/i)).toBeInTheDocument();
    });

    // Successfully unlocked
    expect(screen.getByLabelText('Start voice capture session')).toBeInTheDocument();
  });
});

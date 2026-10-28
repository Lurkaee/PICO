import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryInspector } from '../components/memory/MemoryInspector';
import { api } from '../api/client';

vi.mock('../api/client', () => ({
  api: {
    getSemanticFacts: vi.fn(),
    searchSemanticFacts: vi.fn(),
    forgetSemanticFact: vi.fn(),
    getWorkingMemory: vi.fn(),
    getProceduralMemory: vi.fn(),
    getMemoryStatus: vi.fn(),
  },
}));

describe('MemoryInspector Component', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('refreshes the current search and clears to an unfiltered list', async () => {
    vi.mocked(api.getSemanticFacts).mockResolvedValue([]);
    vi.mocked(api.searchSemanticFacts).mockResolvedValue([]);
    render(<MemoryInspector />);
    await waitFor(() => expect(screen.getByText('Refresh')).not.toBeDisabled());

    const input = screen.getByPlaceholderText(/Search semantic memory/);
    fireEvent.change(input, { target: { value: '  Python  ' } });
    fireEvent.click(screen.getByRole('button', { name: 'Search' }));
    await waitFor(() => expect(api.searchSemanticFacts).toHaveBeenCalledWith('Python', 20));
    await waitFor(() => expect(screen.getByText('Refresh')).not.toBeDisabled());
    fireEvent.click(screen.getByText('Refresh'));
    await waitFor(() => expect(api.searchSemanticFacts).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.getByText('Refresh')).not.toBeDisabled());

    fireEvent.click(screen.getByText('Clear'));
    await waitFor(() => expect(api.getSemanticFacts).toHaveBeenCalledTimes(2));
    expect(api.getSemanticFacts).toHaveBeenLastCalledWith(50, 0);
    expect(api.searchSemanticFacts).toHaveBeenCalledTimes(2);
    expect(input).toHaveValue('');
  });

  it('renders semantic facts and distinguishes observed truth from inferred hypotheses', async () => {
    vi.mocked(api.getSemanticFacts).mockResolvedValueOnce([
      {
        id: 'fact_1',
        subject: 'User',
        predicate: 'prefers_language',
        object: 'Python',
        confidence: 1.0,
        source_provenance: 'explicit_user_statement',
        data_classification: 'INTERNAL',
        verified: true,
        is_inferred: false,
        created_at: '2026-10-22T08:00:00Z',
      },
      {
        id: 'fact_2',
        subject: 'User',
        predicate: 'likely_working_on',
        object: 'FastAPI Backend',
        confidence: 0.72,
        source_provenance: 'file_edit_inference',
        data_classification: 'INTERNAL',
        verified: false,
        is_inferred: true,
        created_at: '2026-10-22T08:05:00Z',
      },
    ]);

    render(<MemoryInspector />);

    await waitFor(() => {
      expect(screen.getByText('Python')).toBeInTheDocument();
      expect(screen.getByText('FastAPI Backend')).toBeInTheDocument();
    });

    expect(screen.getByText('Observed Ground Truth')).toBeInTheDocument();
    expect(screen.getByText('Inferred Hypothesis')).toBeInTheDocument();
  });

  it('requires confirmation before forgetting a semantic fact and reflects deletion', async () => {
    vi.mocked(api.getSemanticFacts).mockResolvedValueOnce([
      {
        id: 'fact_to_forget',
        subject: 'Operator',
        predicate: 'temporary_note',
        object: 'Meeting at 3PM',
        confidence: 0.9,
        source_provenance: 'note',
        data_classification: 'INTERNAL',
        verified: true,
        is_inferred: false,
        created_at: '2026-10-22T08:00:00Z',
      },
    ]);

    vi.mocked(api.forgetSemanticFact).mockResolvedValueOnce({ forgotten: true });

    render(<MemoryInspector />);

    await waitFor(() => {
      expect(screen.getByText('Meeting at 3PM')).toBeInTheDocument();
    });

    // Click trash button
    const trashBtn = screen.getByLabelText('Forget fact');
    fireEvent.click(trashBtn);

    // Confirmation prompt appears
    expect(screen.getByText('Confirm forget?')).toBeInTheDocument();
    expect(api.forgetSemanticFact).not.toHaveBeenCalled();

    // Mock next fetch after deletion returns empty list
    vi.mocked(api.getSemanticFacts).mockResolvedValueOnce([]);

    // Click Forget confirmation button
    const confirmBtn = screen.getByRole('button', { name: 'Forget' });
    fireEvent.click(confirmBtn);

    await waitFor(() => {
      expect(api.forgetSemanticFact).toHaveBeenCalledWith('fact_to_forget');
    });
  });
});

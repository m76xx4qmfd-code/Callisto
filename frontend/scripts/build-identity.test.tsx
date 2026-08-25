import React from 'react'
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { BuildIdentity } from '../src/components/BuildIdentity'


afterEach(cleanup)

describe('BuildIdentity', () => {
  it('renders the Callisto wordmark with a UTC timestamp build ID directly beneath it', () => {
    render(<BuildIdentity />)

    const identity = screen.getByLabelText('Callisto build identity')
    expect(identity.textContent).toContain('CALLISTO')
    expect(identity.textContent).toMatch(/BUILD \d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC/)

    const wordmark = screen.getByText('CALLISTO')
    const buildLabel = screen.getByText(/BUILD \d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC/)
    expect(wordmark.parentElement).toBe(identity)
    expect(buildLabel.parentElement).toBe(identity)
    expect(buildLabel.className).toContain('text-[9px]')
    expect(identity.className).toContain('flex-col')
  })
})

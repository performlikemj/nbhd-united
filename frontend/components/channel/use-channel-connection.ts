"use client";

import { useState } from "react";

import type { LineLinkResponse, TelegramLinkResponse } from "@/lib/api";
import { getErrorMessage } from "@/lib/errors";
import {
  useGenerateLineLinkMutation,
  useGenerateTelegramLinkMutation,
  useLineStatusQuery,
  useTelegramStatusQuery,
} from "@/lib/queries";

function hasErrorCode(error: unknown, code: string): boolean {
  if (!(error instanceof Error)) return false;
  try {
    const body = JSON.parse(error.message) as { code?: unknown };
    return body.code === code;
  } catch {
    return false;
  }
}

export function useTelegramConnection(pairingOpen: boolean) {
  const [link, setLink] = useState<TelegramLinkResponse | null>(null);
  const [generationError, setGenerationError] = useState<string | null>(null);
  const statusQuery = useTelegramStatusQuery(true, pairingOpen && Boolean(link));
  const generateMutation = useGenerateTelegramLinkMutation();

  const generate = async () => {
    setGenerationError(null);
    generateMutation.reset();
    try {
      setLink(await generateMutation.mutateAsync());
    } catch (error) {
      setGenerationError(getErrorMessage(error));
    }
  };

  return {
    link,
    generationError,
    generate,
    isGenerating: generateMutation.isPending,
    status: statusQuery.data,
    statusError: statusQuery.isError ? getErrorMessage(statusQuery.error) : null,
    statusReady: statusQuery.isFetchedAfterMount && !statusQuery.isError,
    retryStatus: statusQuery.refetch,
  };
}

export function useLineConnection(pairingOpen: boolean) {
  const [link, setLink] = useState<LineLinkResponse | null>(null);
  const [generationError, setGenerationError] = useState<string | null>(null);
  const statusQuery = useLineStatusQuery(true, pairingOpen && Boolean(link));
  const generateMutation = useGenerateLineLinkMutation();

  const generate = async () => {
    setGenerationError(null);
    generateMutation.reset();
    try {
      setLink(await generateMutation.mutateAsync());
    } catch (error) {
      setGenerationError(getErrorMessage(error));
      if (hasErrorCode(error, "line_quota_exhausted")) {
        await statusQuery.refetch();
      }
    }
  };

  return {
    link,
    generationError,
    generate,
    isGenerating: generateMutation.isPending,
    status: statusQuery.data,
    statusError: statusQuery.isError ? getErrorMessage(statusQuery.error) : null,
    statusReady: statusQuery.isFetchedAfterMount && !statusQuery.isError,
    retryStatus: statusQuery.refetch,
  };
}

export type IncidentFingerprint = {
  service: string;
  panicType: string;
  topFrame: string;
  commitHash: string;
};

export type ResolutionPackage = {
  fingerprint: IncidentFingerprint;
  rootCause: string;
  patch: string;
};
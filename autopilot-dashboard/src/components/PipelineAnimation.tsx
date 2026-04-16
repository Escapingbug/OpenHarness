/**
 * PipelineAnimation — visualizes the autopilot pipeline stages.
 * A data packet travels through: QUEUE → PREPARE → RUN → VERIFY → MERGE
 * Communicates the "self-evolution pipeline" idea.
 * Uses SMIL animations — no JS loops needed.
 */
export function PipelineAnimation() {
  const stages = [
    { x: 30, label: "QUEUE" },
    { x: 95, label: "PREP" },
    { x: 160, label: "RUN" },
    { x: 225, label: "CHECK" },
    { x: 290, label: "MERGE" },
  ];

  const motionPath = "M 30 50 L 95 50 L 160 50 L 225 50 L 290 50";

  return (
    <svg
      viewBox="0 0 320 100"
      xmlns="http://www.w3.org/2000/svg"
      aria-label="Autopilot pipeline"
      style={{ width: "100%", height: "auto" }}
    >
      <defs>
        <radialGradient id="pipe-glow" cx="50%" cy="50%" r="50%">
          <stop offset="0%" stopColor="#00d4aa" stopOpacity="0.8" />
          <stop offset="50%" stopColor="#00d4aa" stopOpacity="0.3" />
          <stop offset="100%" stopColor="#00d4aa" stopOpacity="0" />
        </radialGradient>
      </defs>

      {/* Rail */}
      <line x1="30" y1="50" x2="290" y2="50"
        stroke="#00d4aa" strokeOpacity="0.15" strokeWidth="1" strokeDasharray="3 5">
        <animate attributeName="stroke-dashoffset" values="0;-16" dur="1.5s" repeatCount="indefinite" />
      </line>

      {/* Stage nodes */}
      {stages.map((stage, i) => (
        <g key={stage.label} transform={`translate(${stage.x}, 50)`}>
          {/* Node circle */}
          <circle r="14" fill="#0a0a0a" stroke="#00d4aa" strokeOpacity="0.4" strokeWidth="1" />
          {/* Corner ticks */}
          <line x1="-10" y1="-10" x2="-7" y2="-10" stroke="#00d4aa" strokeOpacity="0.6" strokeWidth="1" />
          <line x1="-10" y1="-10" x2="-10" y2="-7" stroke="#00d4aa" strokeOpacity="0.6" strokeWidth="1" />
          <line x1="10" y1="-10" x2="7" y2="-10" stroke="#00d4aa" strokeOpacity="0.6" strokeWidth="1" />
          <line x1="10" y1="-10" x2="10" y2="-7" stroke="#00d4aa" strokeOpacity="0.6" strokeWidth="1" />
          <line x1="-10" y1="10" x2="-7" y2="10" stroke="#00d4aa" strokeOpacity="0.6" strokeWidth="1" />
          <line x1="-10" y1="10" x2="-10" y2="7" stroke="#00d4aa" strokeOpacity="0.6" strokeWidth="1" />
          <line x1="10" y1="10" x2="7" y2="10" stroke="#00d4aa" strokeOpacity="0.6" strokeWidth="1" />
          <line x1="10" y1="10" x2="10" y2="7" stroke="#00d4aa" strokeOpacity="0.6" strokeWidth="1" />
          {/* Inner dot */}
          <circle r="3" fill="#00d4aa">
            <animate attributeName="r" values="3;4.5;3" dur="2.4s"
              begin={`${i * 0.5}s`} repeatCount="indefinite" />
            <animate attributeName="opacity" values="0.4;1;0.4" dur="2.4s"
              begin={`${i * 0.5}s`} repeatCount="indefinite" />
          </circle>
          {/* Arrival pulse */}
          <circle r="14" fill="none" stroke="#00d4aa" strokeWidth="1">
            <animate attributeName="r" values="14;24" dur="3s"
              begin={`${i * 0.8}s`} repeatCount="indefinite" />
            <animate attributeName="opacity" values="0.5;0" dur="3s"
              begin={`${i * 0.8}s`} repeatCount="indefinite" />
          </circle>
          {/* Label */}
          <text x="0" y="30" fontSize="7" fill="#00d4aa" fillOpacity="0.6"
            textAnchor="middle" fontFamily="JetBrains Mono, monospace"
            letterSpacing="1.5" fontWeight="600">
            {stage.label}
          </text>
        </g>
      ))}

      {/* Traveling glow */}
      <circle r="14" fill="url(#pipe-glow)" opacity="0.7">
        <animateMotion dur="4s" repeatCount="indefinite" path={motionPath}
          keyPoints="0;0.24;0.25;0.49;0.5;0.74;0.75;0.99;1"
          keyTimes="0;0.18;0.22;0.38;0.42;0.58;0.62;0.78;1"
          calcMode="spline"
          keySplines="0.4 0 0.6 1;0 0 1 1;0.4 0 0.6 1;0 0 1 1;0.4 0 0.6 1;0 0 1 1;0.4 0 0.6 1;0 0 1 1" />
      </circle>
      <circle r="4" fill="#00d4aa">
        <animateMotion dur="4s" repeatCount="indefinite" path={motionPath}
          keyPoints="0;0.24;0.25;0.49;0.5;0.74;0.75;0.99;1"
          keyTimes="0;0.18;0.22;0.38;0.42;0.58;0.62;0.78;1"
          calcMode="spline"
          keySplines="0.4 0 0.6 1;0 0 1 1;0.4 0 0.6 1;0 0 1 1;0.4 0 0.6 1;0 0 1 1;0.4 0 0.6 1;0 0 1 1" />
      </circle>

      {/* Caption */}
      <text x="160" y="93" fontSize="7" fill="#00d4aa" fillOpacity="0.45"
        textAnchor="middle" fontFamily="JetBrains Mono, monospace" letterSpacing="2.5">
        AUTOPILOT · PIPELINE
      </text>
    </svg>
  );
}

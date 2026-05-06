package publisher

import "time"

type SessionConfig struct {
	URL                 string
	APIKey              string
	APISecret           string
	Identity            string
	Room                string
	FPS                 float64
	H26xStreamingFormat string
	ExitAfterPublish    bool
	ReconnectAttempts   int
	ReconnectDelay      time.Duration
	Targets             []PublishTarget
}

type PublishTarget struct {
	Raw     string
	Codec   string
	Name    string
	Address string
	Network string
	Width   uint32
	Height  uint32
}

func (t PublishTarget) HasDimensions() bool {
	return t.Width > 0 && t.Height > 0
}

type PublishGroupKind string

const (
	PublishGroupIndependent PublishGroupKind = "independent"
	PublishGroupSimulcast   PublishGroupKind = "simulcast"
)

type PublishGroup struct {
	Kind    PublishGroupKind
	Name    string
	Codec   string
	Targets []PublishTarget
}

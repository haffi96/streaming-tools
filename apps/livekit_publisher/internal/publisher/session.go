package publisher

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log"
	"net"
	"os/signal"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/livekit/protocol/livekit"
	protologger "github.com/livekit/protocol/logger"
	"github.com/pion/rtcp"
	"github.com/pion/webrtc/v4"

	lksdk "github.com/livekit/server-sdk-go/v2"
)

type sessionState struct {
	mu             sync.Mutex
	remainingGroup map[string]struct{}
	exitAfter      bool
	done           chan struct{}
	closed         bool
}

func newSessionState(exitAfter bool, groups []PublishGroup) *sessionState {
	remaining := make(map[string]struct{}, len(groups))
	for _, group := range groups {
		remaining[group.Name] = struct{}{}
	}
	return &sessionState{
		remainingGroup: remaining,
		exitAfter:      exitAfter,
		done:           make(chan struct{}),
	}
}

func (s *sessionState) markGroupComplete(name string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	delete(s.remainingGroup, name)
	if s.exitAfter && len(s.remainingGroup) == 0 && !s.closed {
		s.closed = true
		close(s.done)
	}
}

func (s *sessionState) signalStop() {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.closed {
		return
	}
	s.closed = true
	close(s.done)
}

func SetLogger() {
	logConfig := &protologger.Config{
		Level: "info",
		ComponentLevels: map[string]string{
			"pion": "error",
		},
	}
	protologger.InitFromConfig(logConfig, "livekit_publisher")
	lksdk.SetLogger(protologger.GetLogger())
}

func Run(ctx context.Context, cfg SessionConfig) error {
	if len(cfg.Targets) == 0 {
		return errors.New("at least one --publish target is required")
	}
	if err := validateStreamingFormat(cfg.H26xStreamingFormat); err != nil {
		return err
	}

	groups, err := GroupPublishTargets(cfg.Targets)
	if err != nil {
		return err
	}

	runCtx, cancel := context.WithCancel(ctx)
	defer cancel()

	state := newSessionState(cfg.ExitAfterPublish, groups)

	roomCB := &lksdk.RoomCallback{
		OnReconnecting: func() { log.Println("reconnecting to room") },
		OnReconnected:  func() { log.Println("reconnected to room") },
		OnDisconnected: func() {
			log.Println("disconnected from room")
			cancel()
			state.signalStop()
		},
	}

	room, err := lksdk.ConnectToRoom(cfg.URL, lksdk.ConnectInfo{
		APIKey:              cfg.APIKey,
		APISecret:           cfg.APISecret,
		RoomName:            cfg.Room,
		ParticipantIdentity: cfg.Identity,
	}, roomCB)
	if err != nil {
		return err
	}
	defer room.Disconnect()

	log.Printf("connected to room=%s identity=%s", room.Name(), cfg.Identity)

	for _, group := range groups {
		group := group
		go runPublishGroupManager(runCtx, room, cfg, group, state)
	}

	sigCtx, stop := signal.NotifyContext(runCtx, syscall.SIGINT, syscall.SIGTERM, syscall.SIGQUIT)
	defer stop()

	select {
	case <-sigCtx.Done():
		return nil
	case <-state.done:
		return nil
	}
}

func runPublishGroupManager(ctx context.Context, room *lksdk.Room, cfg SessionConfig, group PublishGroup, state *sessionState) {
	attempt := 0

	for {
		if ctx.Err() != nil {
			return
		}

		attempt++
		switch group.Kind {
		case PublishGroupIndependent:
			target := group.Targets[0]
			log.Printf("publish manager attempt=%d track=%s address=%s", attempt, target.Name, target.Address)
			reconnect, err := runIndependentPublishAttempt(ctx, room, cfg, target)
			if err != nil {
				log.Printf("publish manager failed track=%s attempt=%d: %v", target.Name, attempt, err)
			} else if !reconnect {
				state.markGroupComplete(group.Name)
				return
			}
		case PublishGroupSimulcast:
			log.Printf("publish manager attempt=%d simulcast=%s layers=%d", attempt, group.Name, len(group.Targets))
			reconnect, err := runSimulcastPublishAttempt(ctx, room, cfg, group)
			if err != nil {
				log.Printf("publish manager failed simulcast=%s attempt=%d: %v", group.Name, attempt, err)
			} else if !reconnect {
				state.markGroupComplete(group.Name)
				return
			}
		default:
			log.Printf("publish manager got unsupported group kind=%s", group.Kind)
			state.markGroupComplete(group.Name)
			return
		}

		if ctx.Err() != nil {
			return
		}
		retriesUsed := attempt - 1
		if !shouldRetry(cfg.ReconnectAttempts, retriesUsed) {
			log.Printf("publish manager giving up group=%s after %d attempts", group.Name, attempt)
			state.markGroupComplete(group.Name)
			return
		}
		if !waitForReconnect(ctx, cfg.ReconnectDelay) {
			return
		}
	}
}

func runIndependentPublishAttempt(ctx context.Context, room *lksdk.Room, cfg SessionConfig, target PublishTarget) (bool, error) {
	conn, err := net.Dial(target.Network, target.Address)
	if err != nil {
		return true, fmt.Errorf("failed to connect %s to %s: %w", target.Name, target.Address, err)
	}

	done := make(chan struct{})
	var once sync.Once
	notifyDone := func() {
		once.Do(func() {
			close(done)
		})
	}

	var pub *lksdk.LocalTrackPublication
	track, err := buildReaderTrack(conn, target.Codec, cfg, notifyDone)
	if err != nil {
		_ = conn.Close()
		return false, err
	}

	options := &lksdk.TrackPublicationOptions{
		Name:                target.Name,
		Source:              livekit.TrackSource_CAMERA,
		AttachUserTimestamp: true,
		AttachFrameId:       true,
	}
	if target.HasDimensions() {
		options.VideoWidth = int(target.Width)
		options.VideoHeight = int(target.Height)
	}

	pub, err = room.LocalParticipant.PublishTrack(track, options)
	if err != nil {
		track.Close()
		return true, fmt.Errorf("failed to publish track %s: %w", target.Name, err)
	}

	log.Printf("published track name=%s codec=%s address=%s", target.Name, strings.ToUpper(target.Codec), target.Address)

	select {
	case <-ctx.Done():
		cleanupPublication(room, pub, track)
		return false, nil
	case <-done:
		log.Printf("track ended name=%s address=%s", target.Name, target.Address)
		cleanupPublication(room, pub, track)
		return true, nil
	}
}

func runSimulcastPublishAttempt(ctx context.Context, room *lksdk.Room, cfg SessionConfig, group PublishGroup) (bool, error) {
	qualities := []livekit.VideoQuality{
		livekit.VideoQuality_LOW,
		livekit.VideoQuality_HIGH,
	}
	if len(group.Targets) == 3 {
		qualities = []livekit.VideoQuality{
			livekit.VideoQuality_LOW,
			livekit.VideoQuality_MEDIUM,
			livekit.VideoQuality_HIGH,
		}
	}

	done := make(chan struct{})
	var once sync.Once
	notifyDone := func() {
		once.Do(func() {
			close(done)
		})
	}

	tracks := make([]*lksdk.LocalTrack, 0, len(group.Targets))
	conns := make([]io.Closer, 0, len(group.Targets))
	for idx, target := range group.Targets {
		conn, err := net.Dial(target.Network, target.Address)
		if err != nil {
			closeClosers(conns)
			closeTracks(tracks)
			return true, fmt.Errorf("failed to connect simulcast layer %s to %s: %w", target.Name, target.Address, err)
		}
		conns = append(conns, conn)

		layer := &livekit.VideoLayer{
			Quality: qualities[idx],
			Width:   target.Width,
			Height:  target.Height,
		}
		track, err := buildReaderTrack(
			conn,
			target.Codec,
			cfg,
			notifyDone,
			lksdk.ReaderTrackWithSampleOptions(lksdk.WithSimulcast(group.Name, layer)),
		)
		if err != nil {
			_ = conn.Close()
			closeClosers(conns)
			closeTracks(tracks)
			return false, err
		}
		tracks = append(tracks, track)
	}

	pub, err := room.LocalParticipant.PublishSimulcastTrack(tracks, &lksdk.TrackPublicationOptions{
		Name:                group.Name,
		Source:              livekit.TrackSource_CAMERA,
		AttachUserTimestamp: true,
		AttachFrameId:       true,
	})
	if err != nil {
		closeClosers(conns)
		closeTracks(tracks)
		return true, fmt.Errorf("failed to publish simulcast track %s: %w", group.Name, err)
	}

	log.Printf("published simulcast track name=%s codec=%s layers=%d", group.Name, strings.ToUpper(group.Codec), len(group.Targets))

	select {
	case <-ctx.Done():
		cleanupSimulcastPublication(room, pub, tracks)
		return false, nil
	case <-done:
		log.Printf("simulcast track ended name=%s", group.Name)
		cleanupSimulcastPublication(room, pub, tracks)
		return true, nil
	}
}

func buildReaderTrack(in io.ReadCloser, codec string, cfg SessionConfig, onComplete func(), extraOpts ...lksdk.ReaderSampleProviderOption) (*lksdk.LocalTrack, error) {
	opts := []lksdk.ReaderSampleProviderOption{
		lksdk.ReaderTrackWithPacketTrailer(true),
		lksdk.ReaderTrackWithOnWriteComplete(onComplete),
		lksdk.ReaderTrackWithRTCPHandler(func(packet rtcp.Packet) {
			switch packet.(type) {
			case *rtcp.PictureLossIndication:
				log.Printf("received PLI codec=%s", strings.ToUpper(codec))
			}
		}),
	}
	if cfg.FPS > 0 {
		frameDuration := time.Second / time.Duration(cfg.FPS)
		opts = append(opts, lksdk.ReaderTrackWithFrameDuration(frameDuration))
	}
	switch cfg.H26xStreamingFormat {
	case "annex-b":
		opts = append(opts, lksdk.ReaderTrackWithH26xStreamingFormat(lksdk.H26xStreamingFormatAnnexB))
	case "length-prefixed":
		opts = append(opts, lksdk.ReaderTrackWithH26xStreamingFormat(lksdk.H26xStreamingFormatLengthPrefixed))
	default:
		return nil, fmt.Errorf("unsupported h26x streaming format: %s", cfg.H26xStreamingFormat)
	}
	opts = append(opts, extraOpts...)
	return lksdk.NewLocalReaderTrack(in, mimeTypeForCodec(codec), opts...)
}

func mimeTypeForCodec(codec string) string {
	switch codec {
	case "h264":
		return webrtc.MimeTypeH264
	case "h265":
		return webrtc.MimeTypeH265
	default:
		return ""
	}
}

func validateStreamingFormat(format string) error {
	switch format {
	case "annex-b", "length-prefixed":
		return nil
	default:
		return fmt.Errorf("unsupported h26x streaming format: %s", format)
	}
}

func shouldRetry(reconnectAttempts int, retriesUsed int) bool {
	if reconnectAttempts < 0 {
		return true
	}
	return retriesUsed < reconnectAttempts
}

func waitForReconnect(ctx context.Context, delay time.Duration) bool {
	if delay <= 0 {
		delay = 3 * time.Second
	}
	timer := time.NewTimer(delay)
	defer timer.Stop()

	select {
	case <-ctx.Done():
		return false
	case <-timer.C:
		return true
	}
}

func cleanupPublication(room *lksdk.Room, pub *lksdk.LocalTrackPublication, track *lksdk.LocalTrack) {
	if pub != nil {
		if err := room.LocalParticipant.UnpublishTrack(pub.SID()); err != nil {
			log.Printf("failed to unpublish track sid=%s: %v", pub.SID(), err)
		}
	}
	if track != nil {
		track.Close()
	}
}

func cleanupSimulcastPublication(room *lksdk.Room, pub *lksdk.LocalTrackPublication, tracks []*lksdk.LocalTrack) {
	if pub != nil {
		if err := room.LocalParticipant.UnpublishTrack(pub.SID()); err != nil {
			log.Printf("failed to unpublish simulcast track sid=%s: %v", pub.SID(), err)
		}
	}
	closeTracks(tracks)
}

func closeTracks(tracks []*lksdk.LocalTrack) {
	for _, track := range tracks {
		if track != nil {
			track.Close()
		}
	}
}

func closeClosers(closers []io.Closer) {
	for _, closer := range closers {
		if closer != nil {
			_ = closer.Close()
		}
	}
}

func LoadConfig(
	url string,
	apiKey string,
	apiSecret string,
	identity string,
	room string,
	fps float64,
	format string,
	exitAfter bool,
	reconnectAttempts int,
	reconnectDelay time.Duration,
	publishURLs []string,
) (SessionConfig, error) {
	if url == "" {
		return SessionConfig{}, errors.New("--url is required")
	}
	if apiKey == "" {
		return SessionConfig{}, errors.New("--api-key is required")
	}
	if apiSecret == "" {
		return SessionConfig{}, errors.New("--api-secret is required")
	}
	if identity == "" {
		return SessionConfig{}, errors.New("--identity is required")
	}
	if room == "" {
		return SessionConfig{}, errors.New("--room is required")
	}

	targets := make([]PublishTarget, 0, len(publishURLs))
	for _, raw := range publishURLs {
		target, err := ParsePublishTarget(raw)
		if err != nil {
			return SessionConfig{}, err
		}
		targets = append(targets, target)
	}

	return SessionConfig{
		URL:                 url,
		APIKey:              apiKey,
		APISecret:           apiSecret,
		Identity:            identity,
		Room:                room,
		FPS:                 fps,
		H26xStreamingFormat: format,
		ExitAfterPublish:    exitAfter,
		ReconnectAttempts:   reconnectAttempts,
		ReconnectDelay:      reconnectDelay,
		Targets:             targets,
	}, nil
}

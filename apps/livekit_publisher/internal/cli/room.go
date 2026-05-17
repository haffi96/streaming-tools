package cli

import (
	"context"
	"time"

	"livekit_publisher/internal/publisher"

	"github.com/urfave/cli/v3"
)

func roomCommand() *cli.Command {
	return &cli.Command{
		Name:  "room",
		Usage: "Join a room as a publisher participant",
		Commands: []*cli.Command{
			{
				Name:      "join",
				Usage:     "Join a room and publish TCP camera streams",
				UsageText: "livekit_publisher room join [OPTIONS]",
				Action:    joinRoom,
				Flags: []cli.Flag{
					&cli.StringFlag{Name: "url", Usage: "LiveKit server websocket URL", Required: true},
					&cli.StringFlag{Name: "api-key", Usage: "LiveKit API key", Required: true},
					&cli.StringFlag{Name: "api-secret", Usage: "LiveKit API secret", Required: true},
					&cli.StringFlag{Name: "identity", Usage: "Participant identity", Required: true},
					&cli.StringFlag{Name: "room", Usage: "Room name", Required: true},
					&cli.StringSliceFlag{
						Name:  "publish",
						Usage: "Publish target in the format <codec>://<name>@<host:port>[/<width>x<height>]; repeat for multiple tracks or simulcast layers",
					},
					&cli.FloatFlag{
						Name:  "fps",
						Usage: "Frame rate used for pacing pre-encoded video",
					},
					&cli.StringFlag{
						Name:  "h26x-streaming-format",
						Usage: "H.26x stream format: annex-b or length-prefixed",
						Value: "annex-b",
					},
					&cli.BoolFlag{
						Name:  "exit-after-publish",
						Usage: "Exit after all published tracks finish",
					},
					&cli.IntFlag{
						Name:  "reconnect-attempts",
						Usage: "Number of reconnect attempts after a source ends or is unavailable (-1 retries forever, default: -1)",
						Value: -1,
					},
					&cli.DurationFlag{
						Name:  "reconnect-delay",
						Usage: "Delay between reconnect attempts",
						Value: 3 * time.Second,
					},
				},
			},
		},
	}
}

func joinRoom(ctx context.Context, cmd *cli.Command) error {
	cfg, err := publisher.LoadConfig(
		cmd.String("url"),
		cmd.String("api-key"),
		cmd.String("api-secret"),
		cmd.String("identity"),
		cmd.String("room"),
		cmd.Float("fps"),
		cmd.String("h26x-streaming-format"),
		cmd.Bool("exit-after-publish"),
		cmd.Int("reconnect-attempts"),
		cmd.Duration("reconnect-delay"),
		cmd.StringSlice("publish"),
	)
	if err != nil {
		return err
	}
	return publisher.Run(ctx, cfg)
}

package cli

import (
	"context"

	"livekit-publisher/internal/publisher"

	"github.com/urfave/cli/v3"
)

func NewApp() *cli.Command {
	return &cli.Command{
		Name:                  "livekit-publisher",
		Usage:                 "Publish H.264/H.265 TCP streams to LiveKit",
		EnableShellCompletion: true,
		Before: func(ctx context.Context, cmd *cli.Command) (context.Context, error) {
			publisher.SetLogger()
			return ctx, nil
		},
		Commands: []*cli.Command{
			roomCommand(),
		},
	}
}

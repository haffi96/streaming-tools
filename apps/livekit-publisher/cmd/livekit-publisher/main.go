package main

import (
	"context"
	"fmt"
	"os"

	appcli "livekit-publisher/internal/cli"
)

func main() {
	app := appcli.NewApp()
	if err := app.Run(context.Background(), os.Args); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

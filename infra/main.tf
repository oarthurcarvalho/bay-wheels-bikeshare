terraform {
  # 1.10 é a primeira com trava do state no próprio S3 (use_lockfile)
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # O bucket do state foi criado à mão: é o único recurso fora do código
  backend "s3" {
    bucket       = "baywheels-tfstate-arthur"
    key          = "coletor/terraform.tfstate"
    region       = "us-east-1"
    encrypt      = true
    use_lockfile = true
  }
}

provider "aws" {
  region = "us-east-1"

  # Entra em todo recurso que aceita tag: é o filtro do custo no Cost Explorer
  default_tags {
    tags = {
      Projeto = "baywheels"
    }
  }
}
